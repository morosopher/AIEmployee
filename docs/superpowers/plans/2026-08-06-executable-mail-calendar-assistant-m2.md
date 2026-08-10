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
exceptions so Task 25 can distinguish an attempted failure from an unsupported operation. `exchange_code()` and
`refresh()` are single provider-call ports: adapters never loop, silently retry, or perform an unconditional
credential upsert after an unknown response. Existing-connection callers must claim through the shared
`OAuthRefreshCoordinator`/started-fence boundary added in Task 27A; a Taskiq or `TransientProviderError` delivery
re-entering after a started call is a zero-provider-call state lookup, not another adapter invocation.

- [ ] **Step 4: Implement provider-neutral use cases and persistence**

Refactor `GoogleConnectionsUseCase` into `ConnectionsUseCase` with an injected fixed adapter mapping. `start()` accepts a provider and non-empty read-capability set; `start_capability_enable()` takes the dependency closure plus all currently enabled capabilities. Persist provider, requested capabilities, PKCE verifier, and optional OIDC nonce hash in `oauth_attempts`.

The callback must consume state once, resolve the adapter recorded on the attempt, save normalized identity/tokens, and set each requested capability to `enabled` only if all required scopes are present; otherwise set `action_required` with `connection_scope_missing`. Preserve a prior refresh-token ciphertext when `OAuthTokenSet.refresh_token is None`. For an existing connected identity, retain the frozen connection/snapshot seam so Tasks 27A–27B can require a coordinator claim and progressive-recovery linkage; do not call `ensure_connection`, issue an unbounded retry, or overwrite credentials with an unconditional upsert.

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

Contract tests must assert `include_granted_scopes=true`, `access_type=offline`, S256 PKCE, callback use of returned scopes, missing-scope `action_required`, refresh-token preservation, single-call unknown classification with no refresh retry/replay, and best-effort revoke.

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

The contract suite must cover the `common` v2 authorization endpoint, S256 PKCE, nonce validation, personal and work/school issuer forms, delegated scope unions, `offline_access`, refresh without a new refresh token, administrator-consent errors, state replay, 429/5xx classification without a second refresh/exchange call, and disconnect behavior that never calls the broad `/me/revokeSignInSessions` or directory-level `oauth2PermissionGrants` APIs. A provider response whose application is unknown must become a durable coordinator fence/needs-attention result; a later Taskiq or transient delivery proves zero provider calls.

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

Add cases for initial seven-day filtering, pagination through `@odata.nextLink`, persisted `@odata.deltaLink`, tombstones with `@removed`, `Prefer: IdType="ImmutableId"` on every message read, folder-specific cursor expiry fallback, Sent coverage, one coordinator-claimed 401 refresh (no adapter-local retry; started/unknown re-entry has zero provider calls), 403 capability loss, 429 `Retry-After`, 5xx, and malformed fields. Fixtures must use `.example.test` addresses and synthetic bodies only.

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
 projection, one coordinator-claimed 401 refresh, 403 scope loss, 429, 5xx, and calendar cursor expiry fallback. Exact event GET
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
separate later 401 chains using the latest refresh token through the shared coordinator (no adapter-local
retry; a started/unknown re-entry has zero provider calls), refresh rejection, second resource 401,
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
- Modify: `backend/src/ai_employee/domain/briefs.py`
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
git add backend/src/ai_employee/domain/calendar_availability.py backend/src/ai_employee/domain/settings.py backend/src/ai_employee/domain/briefs.py backend/src/ai_employee/application/use_cases/calendar_proposals.py backend/src/ai_employee/workers/prepare_calendar_restore.py backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/agents/daily_brief/nodes.py backend/src/ai_employee/workers/conversation.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/unit/application/test_calendar_proposals.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/agents/test_daily_brief.py backend/tests/integration/api/test_conversations.py
git commit -m "feat: add calendar change proposals"
~~~

### Task 16A: Harden calendar proposal trust boundaries and rotate event-field AAD

**Files:**
- Create: `backend/migrations/versions/20260809_0019_calendar_event_field_aad_v2.py` — forward-only CalendarEvent field-AAD versioning and scoped resync marker migration.
- Modify: `backend/migrations/env.py` — install the frozen 0019 typed guard as an opt-in
  `on_version_apply` final-transaction check without changing other revisions, and make the outer online Alembic
  boundary acquire the management-database lifecycle lease, common target-scoped maintenance lock/check, then the
  fixed schema-lifecycle exclusive lock, holding them in that order through transaction commit/rollback.
- Create: `backend/src/ai_employee/infrastructure/db/database_maintenance.py` — exact signed-system-ID conversion,
  low/high-bit target digest/lock vectors,
  management-database lifecycle lease, database-wide maintenance/call-authority catalog parsers, and typed
  owner-write plus database-lifecycle admission helper. Task 27D extends the same parser/admission model with the
  completion GUC; it does not create a parallel authority path.
- Create: `backend/src/ai_employee/cli/database_maintenance.py` — lifecycle wrapper used by ordinary migration and
  protected non-production reset; Task 27D extends this same CLI with restore execution rather than creating a
  second authority path.
- Create: `backend/src/ai_employee/application/ports/calendar_aad_migration_guard.py` — final typed
  `before_mutation | before_commit` protocol, runtime validation, and built-in zero-bootstrap contract consumed
  unchanged by Task 27C.
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_availability.py` — two-short-transaction availability read/save adapter; it returns frozen DTOs and never performs candidate computation.
- Create: `backend/tests/integration/m2/test_calendar_availability_repository.py` — fixed-query, minimal-projection, freshness-clock, no-open-transaction, large-event-set, and CAS coverage.
- Modify: `backend/src/ai_employee/infrastructure/db/models/sources.py` — independent description/location AAD versions and four-column atomic constraints.
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py` — v2-only CalendarEvent field encryption with the complete calendar identity.
- Modify: `backend/src/ai_employee/application/use_cases/calendar_proposals.py` — independent freshness observation, frozen availability DTO/port, validated confirmation reconstruction, submission readiness, and shared idempotency-key validation.
- Modify: `backend/src/ai_employee/domain/calendar_availability.py` — filtered, sorted, merged buffered intervals and monotonic candidate scanning.
- Modify: `backend/src/ai_employee/workers/prepare_calendar_restore.py` — exact task-input shape and canonical UUID/idempotency validation before resolver or provider access.
- Modify: `backend/src/ai_employee/application/ports/encryption.py` — public read-only `key_version` contract
  plus stable key-version and decryption-boundary errors for single-key AEAD consumers.
- Modify: `backend/src/ai_employee/infrastructure/security/encryption.py` — implement the public read-only
  `key_version` property and reject persisted key-version mismatch before invoking AES-GCM.
- Modify: `backend/src/ai_employee/application/use_cases/task_execution.py` — add time-valid renewal inputs and document the lease contract consumed by `DurableTaskRunner` renew/terminal/retry writes.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py` — set-based availability projection plus v2-only CalendarEvent field reader.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/task_execution.py` — require an unexpired same-owner lease at renew and every running-task state write.
- Modify: `backend/tests/integration/db/test_migrations.py` — 0019 data, constraint, cursor, required access/refresh
  credential, audit-safe error-code, forward-only migration, schema-lifecycle lock, and maintenance-gate regressions.
- Create: `backend/tests/integration/operations/test_database_maintenance_gate.py` — fixed low/high-bit target digest plus
  management/target lock vectors, database-wide custom-setting/new-session visibility, malformed/role-override
  rejection, lifecycle ordering, reset/drop/create zero-call behavior, and owner-entry zero-write coverage reused by
  Task 27D.
- Modify: `backend/tests/integration/conftest.py` — prove the ordinary session-level `upgrade head` entry keeps
  working through the frozen zero-bootstrap branch and cannot bypass the outer migration lock.
- Modify: `compose.yaml` — keep the migration service on ordinary `upgrade head` through the shared outer Alembic
  management/target/schema and gate/call catalog-admission boundary; no rollout-only artifact is required for fresh/strict-zero
  databases.
- Modify: `justfiles/db.just` — route ordinary `db-upgrade` and the existing non-production `db-reset` through the
  typed lifecycle wrapper; remove direct unguarded `dropdb`/`createdb`, keep exact-target confirmation, and expose
  only the typed injection hook needed later by the dedicated 0019 recipe.
- Modify: `scripts/init-db-roles.sh` — obtain the management/target lifecycle admission before owner grants; Task
  27D later factors restore-time grant functions without changing this wrapper.
- Modify: `scripts/run-e2e-backend.sh` — keep E2E bootstrap on ordinary `upgrade head` and the same lock boundary.
- Modify: `scripts/test-run-e2e-backend.sh` — assert fresh E2E upgrade succeeds without a rollout artifact and
  does not bypass a non-empty affected database, management/target/schema locks, or durable catalog authority.
- Modify: `scripts/test-tooling.sh` — cover ordinary DB recipe/common-lock compatibility, absence of implicit guard
  waivers or invented obsolete-0019 repair/stamp/downgrade recipes, and prove `db-reset` no longer shells out to
  unguarded `dropdb`/`createdb`: one management session must hold the target lifecycle lease across
  check→drop→create→ordinary migration with active gate/call-authority zero lifecycle calls.
- Modify: `scripts/test-deployment.sh` — cover Compose migration-service compatibility with zero-bootstrap and the
  common management/target/schema and gate/call catalog-admission boundary.
- Modify: `backend/tests/integration/google/test_calendar_sync.py` — Google v2 AAD write/decrypt and same-event-ID calendar isolation.
- Modify: `backend/tests/integration/microsoft/test_calendar_sync.py` — Microsoft v2 AAD write/decrypt and same-event-ID calendar isolation.
- Modify: `backend/tests/unit/application/test_calendar_proposals.py` — confirmation partition, validated reconstruction, readiness, historical snapshot, and observed-at tests.
- Modify: `backend/tests/unit/domain/test_calendar_availability.py` — merged-interval and large deterministic result tests.
- Modify: `backend/tests/integration/m2/test_calendar_proposal_versions.py` — exact event-field reads, AAD swap/tamper failures, restore-input fail-closed behavior, and real-runner takeover regression.
- Modify: `backend/tests/integration/workers/test_outbox_dispatch.py` — PostgreSQL renew/finish/retry/internal-failure lease-expiry CAS coverage.
- Modify: `backend/tests/unit/workers/test_execution_lease.py` — runner renewal-clock and rejected CAS contract regressions.
- Modify: `backend/tests/unit/security/test_encryption.py` — public read-only key-version accessor and real
  `AeadCipher` key-version tamper rejection before AES-GCM.

All provider behavior in this task uses Fake readers, synthetic encrypted values, or checked-in contract fixtures. Do not configure or access a real Google or Microsoft account, and do not add approval, `ToolExecution`, or provider-write behavior from Task 18 or later.

Task 16A only implements and verifies code plus migration behavior in synthetic environments. It nevertheless owns the
final 0019 revision logic: the typed guard protocol, mutation-time invocation, built-in fresh/strict-zero branch, and
`on_version_apply` final-transaction invocation are frozen here. It also owns the exact target digest bytes and the
lock order `management lifecycle lock → target lock → schema lifecycle lock`. Every migration entrypoint first holds
the target lifecycle lock from database `postgres`, then obtains target admission, then acquires the independent
fixed `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` before opening its migration transaction; all are released only after
explicit commit/rollback. Task 27D's backup side later uses the same management lifecycle wrapper plus
`pg_advisory_lock_shared(20260806, 143)` across pre-revision,
`pg_dump`, post-revision CAS, and local manifest-last publication. This lifecycle lock does not change the frozen
0019 guard phases or replace the `(20260809, 19)` rollout lease. Before that schema lock, Task 16A also creates the
shared owner-write admission helper: derive `target_identity_digest_v1` from the frozen byte grammar, obtain the
management and target locks, then directly read and validate authoritative database-wide
`ai_employee.maintenance_gate` and `ai_employee.restore_call_authority` rows. Ordinary migration/reset requires no
active or `needs_attention` restore. The existing `db-reset` must move from direct `dropdb`/`createdb` to the same
CLI-held management session and in-process typed lease covering exact check→drop→create→migration; an environment
flag cannot waive it. Malformed, role-specific, non-empty active, conflicting or stale-cached state fails before
DDL/DML/drop/create. Task 27D extends this same helper/CLI with matching generic/sealed restore-attempt handling and
the closed three-state catalog grammar; legacy conversion stays on a disposable target and never writes this catalog.
It must not invent a second lock/GUC grammar. Task 27C may only construct and inject the real
artifact-backed guard; it must not modify this revision, `migrations/env.py`, or the same-revision branch semantics.
Because 0019 forbids old/new CalendarEvent readers or writers from coexisting, passing its focused or repository gates
is not authorization for a rolling deployment and does not prove a production rollout. This task must not stop
production services, apply the production migration, or start a resync. The actual release is an operator-controlled
Task 27A–27E/30 procedure following `docs/operations.md`: keep PostgreSQL/Redis running, stop new ingress and
Calendar scheduling, drain every pre-0019 Caddy/API/Worker/Scheduler reader or writer, run the 0018-compatible
preflight that actively refreshes each distinct affected connection and binds an original deadline or exact
zero/no-deadline state, recompute a current-expiry effective deadline at every later guard, back up the complete
manifest/checksum/dump group and audit only after the typed rollout guard passes, apply 0019, make the v2-only image
available without starting general services, run the dedicated ordinal-aware exact-pair one-off recovery and
`post-resync` audit under the same guard, and only then start API/Worker/Scheduler/Caddy and continue release
verification. If a non-empty sealed window cannot clear markers before the effective deadline, Task 27D owns the
independent `calendar-aad-restore-0018` path to the recorded pre-migration 0018 backup; Task 16A must not alter
generic restore or invent a downgrade path.

Task 16A is the first repository task that creates/publishes `20260809_0019`; before it, no 0019 revision is a
released contract. A development/test database that already contains a hand-applied, unknown or incomplete 0019 is
an obsolete experimental target: first take a forensic backup, then use only the protected `just db-reset` against
the precisely confirmed non-production target. The wrapper must hold the management lifecycle lease across catalog
authority check, drop/recreate and ordinary `upgrade head`; after holder crash, an active/`needs_attention` restore
still yields zero drop/create. Do not add a future repair recipe, rewrite an applied revision, silently stamp, or
fake a downgrade.
If production reports an unknown or obsolete 0019, stop immediately and keep all services/writes disabled; never run
`db-reset`, stamp/revision surgery, direct SQL repair, or downgrade without a newly approved incident/data-disposition
plan.

- [ ] **Step 1: Write failing lease and restore-input trust-boundary tests**

In `backend/tests/integration/workers/test_outbox_dispatch.py`, extend the existing PostgreSQL lease cases with all of these assertions:

~~~python
expired_finish = await store.finish(
    task_id=task_id,
    lease_owner="worker-a",
    status=TaskStatus.SUCCEEDED,
    finished_at=lease_expires_at,
    error_code=None,
)
expired_retry = await store.schedule_retry(
    task_id=retry_task_id,
    lease_owner="worker-a",
    scheduled_at=lease_expires_at,
    retry_available_at=lease_expires_at + timedelta(seconds=5),
    error_code="synthetic_retry",
    attempt_count=1,
)
expired_internal_failure = await store.fail_internal(
    task_id=running_task_id,
    lease_owner="worker-a",
    failed_at=lease_expires_at,
    error_code="task_execution_internal_error",
)

assert expired_finish is False
assert expired_retry is False
assert expired_internal_failure is False
assert await retry_outbox_count(retry_task_id) == 0
~~~

Use a strict `>` boundary: equality with `lease_expires_at` is expired. Preserve the existing CREATED, QUEUED, and RETRY_SCHEDULED unowned `fail_internal` cases as successful safe-precondition failures, while an expired RUNNING row remains untouched. Add a takeover case in which the old owner cannot finish or schedule after expiry and the replacement owner can finish only before its new deadline.

For `TaskExecutionStore.renew()`, add PostgreSQL cases with the exact inputs `task_id`, `lease_owner`, `renewed_at`, and `lease_expires_at`. Renewal succeeds only when the persisted RUNNING row still has the same owner, its current persisted `lease_expires_at` is non-null and strictly later than `renewed_at`, and the requested new `lease_expires_at` is also strictly later than `renewed_at`. Prove all of these boundaries: `renewed_at == old lease_expires_at` fails; an already expired lease fails even when the requested deadline is in the future; a valid unexpired same-owner lease renews; after takeover the old owner cannot renew; and renewal can never resurrect an expired lease.

In `backend/tests/integration/m2/test_calendar_proposal_versions.py`, add malformed persisted inputs for missing keys, extra keys, non-string values, non-canonical UUID spellings, empty/blank/padded/over-255 creation keys, and C0/DEL control characters. Every case must assert that both `resolver.calls` and `reader.calls` remain empty. Accept a valid arbitrary key such as `synthetic-restore-key`; do not require a route-level prefix that Task 17 has not defined.

Add one real `DurableTaskRunner` + `SqlAlchemyTaskExecutionStore` + PostgreSQL regression. Reuse the existing restore source seed and transaction-probing Fake reader, advance the injected clock beyond the first lease during `get_current_event()`, and assert the first runner returns `False`, leaves `result_payload is None`, cannot renew the expired lease, and does not mark the task `succeeded`. Then run a replacement owner with a fresh lease and a Fake provider result; assert it creates exactly one restore proposal, persists a non-null result marker, and is the only attempt allowed to renew or mark the task `succeeded`. In `backend/tests/unit/workers/test_execution_lease.py`, also prove the runner samples its injected clock exactly once at each renewal boundary, passes that sample as `renewed_at`, derives the requested deadline from the same sample, and stops without later writes when renewal returns `False`.

- [ ] **Step 2: Run the lease and restore-input RED tests**

Run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/workers/test_outbox_dispatch.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/workers/test_execution_lease.py -q
~~~

Expected: FAIL because renewal and same-owner RUNNING writes currently ignore the persisted expiry deadline, `renew()` has no `renewed_at` comparison, the real runner can accept that stale CAS, and `_restore_input()` currently accepts extra keys, non-canonical UUID text, and keys outside the shared creation-idempotency boundary.

- [ ] **Step 3: Enforce time-valid leases and validate restore input before resolution**

Change `TaskExecutionStore.renew()` and its SQLAlchemy implementation to accept all four explicit inputs:

~~~python
async def renew(
    self,
    *,
    task_id: UUID,
    lease_owner: str,
    renewed_at: datetime,
    lease_expires_at: datetime,
) -> bool: ...
~~~

Normalize both timestamps and reject a requested `lease_expires_at <= renewed_at`. The conditional UPDATE must require RUNNING status, the same `lease_owner`, `lease_expires_at IS NOT NULL`, and the persisted `lease_expires_at > renewed_at`; a future requested deadline must never revive an expired or replaced lease. In `DurableTaskRunner`, sample the injected clock once per renewal boundary, pass that same normalized value as `renewed_at`, compute `lease_expires_at = renewed_at + lease_duration`, and abort on a `False` CAS without fallback state writes.

For the existing terminal/retry/failure methods in `SqlAlchemyTaskExecutionStore`, add the timestamp-specific lease predicates to their conditional updates:

~~~python
TaskRunModel.lease_expires_at.is_not(None),
TaskRunModel.lease_expires_at > finished_at,
~~~

Use `scheduled_at` in `schedule_retry()`. In `fail_internal()`, keep the CREATED/QUEUED/RETRY_SCHEDULED unowned branch unchanged and place `lease_expires_at IS NOT NULL AND lease_expires_at > failed_at` only inside `same_owner_running`. Update the `TaskExecutionStore` and concrete repository docstrings so `False` from renew/completion means the lease was absent, expired, or replaced; do not add a fallback overwrite after a CAS miss.

In `prepare_calendar_restore.py`, perform all payload validation before `CalendarRestoreReaderResolver.resolve()` and before `CalendarReader.get_current_event()`:

~~~python
_RESTORE_INPUT_KEYS = frozenset(
    {"source_snapshot_id", "creation_idempotency_key"}
)


def _restore_input(payload: Mapping[str, object]) -> tuple[UUID, str]:
    """只接受精确、规范且可重放的恢复准备输入。"""
    if set(payload) != _RESTORE_INPUT_KEYS:
        raise TypeError("calendar.restore.prepare input is invalid")
    raw_snapshot = payload["source_snapshot_id"]
    creation_key = payload["creation_idempotency_key"]
    if not isinstance(raw_snapshot, str) or not isinstance(creation_key, str):
        raise TypeError("calendar.restore.prepare input is invalid")
    parsed_snapshot = UUID(raw_snapshot)
    if str(parsed_snapshot) != raw_snapshot:
        raise ValueError("calendar.restore.prepare snapshot id is not canonical")
    return parsed_snapshot, _idempotency_key(creation_key)
~~~

Move the shared creation-key validator to a callable location in `calendar_proposals.py` that the Worker can import without importing SQLAlchemy or provider code. Preserve the existing non-empty, no-padding, maximum-255 boundary and reject every Unicode `Cc` control character; create/update/restore proposal entry points and `_restore_input()` must call the same validator. This is shape and boundary validation only—there is no fixed prefix requirement.

- [ ] **Step 4: Re-run the lease and restore-input tests GREEN**

Run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/workers/test_outbox_dispatch.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/workers/test_execution_lease.py -q
~~~

Expected: PASS; valid same-owner renewal succeeds, equality/expiry/takeover renewal affects zero rows and cannot resurrect a lease, the runner uses one clock sample per renewal, expired same-owner terminal writes affect zero rows, safe unowned precondition failures still persist, corrupt restore inputs make zero resolver/provider calls, and only the replacement runner can persist the restore result and terminal success.

- [ ] **Step 5: Write failing freshness, availability, transaction-boundary, and confirmation tests**

In `backend/tests/unit/application/test_calendar_proposals.py`, make the Fake calendar port record both timestamps and add future/past searches:

~~~python
await use_case.suggest_times(
    user_id=USER_ID,
    proposal_id=proposal.proposal_id,
    expected_version=proposal.current_version,
    search_start=FUTURE_SEARCH_START,
)

assert source.availability_requests == [
    (NOW, FUTURE_SEARCH_START, CALENDAR_AVAILABILITY_HORIZON_DAYS)
]
~~~

Add the inverse case with `search_start < NOW`; both must use `observed_at == NOW` from the injected clock. Add model tests proving an explicit-confirmation shell rejects overlap between `confirmed_fields` and `required_confirmations`, rejects a missing member from their union, and accepts historical `requires_explicit_confirmation=False` before snapshots with both tuples empty.

Exercise `confirm()`, `_apply_user_changes()` through public `edit()`, and `create_restore()` with inputs that would be invalid if Pydantic validation ran after the update. Assert each path raises rather than persisting a malformed snapshot. Add readiness cases with these exact rules:

- all four shell confirmations are present and `required_confirmations` is empty;
- `title.strip()` and `calendar_id.strip()` are non-empty;
- update/restore require non-empty `target_event_id`, non-empty `base_etag`, non-null `before_snapshot_id`, and non-empty `changed_fields`;
- create requires `target_event_id`, `base_etag`, and `before_snapshot_id` all to be `None`;
- ordinary complete create, update, and restore proposals remain ready, while a fully filled shell remains unready until all four typed confirmations are persisted.

In `backend/tests/integration/m2/test_calendar_availability_repository.py`, seed one user with a future search window whose cursors are fresh at `observed_at`, then a past search window whose cursors are stale at `observed_at`. The first result must be complete and the second partial, proving freshness never derives from `search_start`.

Attach a SQLAlchemy statement counter and run the availability read with 1 and 32 relevant calendar connections. Assert both executions issue the same fixed number of SELECT statements. Inspect the captured event SELECT and assert it projects only `starts_at`, `ends_at`, `all_day`, `transparency`, and `status`; directory/cursor reads project only ownership, capability, scope, freshness, and error-code fields. Assert no statement selects CalendarEvent title, description/location AEAD columns, organizer, attendees, ETag, or provider URL, and assert all qualifying events are returned without `LIMIT` truncation.

Instrument transaction begin/end events, invoke the real availability adapter through `CalendarProposalUseCase.suggest_times()`, and make the injected pure suggestion function assert the active database-transaction count is zero while it computes. Mutate the proposal version between the frozen read and save phases and assert the second short transaction loses the `expected_version` CAS without overwriting the newer proposal.

In `backend/tests/unit/domain/test_calendar_availability.py`, create a deterministic large set containing cancelled/transparent rows, nested/adjacent busy ranges, and at least 10,000 non-truncated busy events. Compare the returned candidates with a small brute-force oracle for the same input; this proves prefiltering/merging preserves correctness without measuring wall-clock time.

- [ ] **Step 6: Run the freshness, availability, and confirmation RED tests**

Run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/unit/application/test_calendar_proposals.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/integration/m2/test_calendar_availability_repository.py backend/tests/integration/m2/test_calendar_proposal_versions.py -q
~~~

Expected: FAIL because `search_start` currently doubles as the freshness observation time, availability performs per-connection directory/cursor queries and materializes ORM rows, candidate overlap scans every busy event, suggestion computation shares the caller's transaction, `model_copy(update=...)` skips validation, and readiness does not enforce operation-specific bindings or all four confirmations.

- [ ] **Step 7: Implement frozen reads, transaction-free computation, CAS save, and validated proposal invariants**

Change the application port to carry independent timestamps:

~~~python
async def get_availability_context(
    self,
    *,
    user_id: UUID,
    observed_at: datetime,
    search_start: datetime,
    horizon_days: int,
) -> CalendarAvailabilityContext | None: ...
~~~

`CalendarProposalUseCase.suggest_times()` must call `observed_at = _aware_utc(self._clock(), field="calendar proposal clock")` once, pass that value independently from the caller's `search_start`, and use a narrow availability persistence port implemented in the new repository module. The adapter performs exactly this sequence:

1. open a short read transaction, load the current proposal/version, target retention facts, settings, capability/directory/cursor projections, and minimal availability-event DTOs, then close the transaction;
2. run `suggest_meeting_times()` after the read transaction has ended;
3. open a new short write transaction and save the next immutable desired snapshot only through `expected_version` CAS.

The write phase must not reuse ORM instances from the read phase. A lost CAS returns the existing stable proposal-version conflict and never retries the computation against a silently changed version.

Rewrite `SqlAlchemyCalendarSyncRepository.get_availability_context()` as set queries: one minimal user-settings projection, one connection/capability projection, one provider-calendar projection, one scoped-cursor projection, and one minimal event projection. Query count must remain fixed as relevant connections grow. Compute `cutoff = observed_at - _AVAILABILITY_FRESHNESS`; use `search_start` only for `window_start/window_end`. Do not load sensitive CalendarEvent ORM rows and do not cap the event result set.

In the domain algorithm, filter cancelled and transparent/free facts first, sort buffered half-open intervals by `(start, end)`, merge overlapping or adjacent intervals, and scan them monotonically while candidates advance:

~~~python
busy_index = 0
for candidate_start, candidate_end in ordered_candidates:
    while busy_index < len(merged_busy) and merged_busy[busy_index][1] <= candidate_start:
        busy_index += 1
    if busy_index < len(merged_busy) and merged_busy[busy_index][0] < candidate_end:
        continue
    accept(candidate_start, candidate_end)
~~~

Do not truncate events before merging. Preserve DST, grid, working-hours, buffer, completeness, and attendee-Free/Busy invariants from Task 16.

Add an after-model validator for explicit shells only:

~~~python
if self.requires_explicit_confirmation:
    confirmed = set(self.confirmed_fields)
    required = set(self.required_confirmations)
    shell = set(_SHELL_CONFIRMATIONS)
    if confirmed & required or confirmed | required != shell:
        raise ValueError("calendar proposal confirmations must partition the shell fields")
~~~

Historical before snapshots with `requires_explicit_confirmation=False` remain readable. Replace validation-sensitive `model_copy(update=...)` calls in `confirm()`, `_apply_user_changes()`, and both reconstruction points in `create_restore()` with `CalendarProposalContent.model_validate()` over a dumped-and-updated mapping:

~~~python
values = content.model_dump(mode="python")
values.update(updates)
updated = CalendarProposalContent.model_validate(values)
~~~

Make `CalendarProposalView.submission_ready` enforce the exact create/update/restore binding rules from the RED tests. This property remains a local edit-readiness signal only; it must not create an approval, task, ToolExecution, or external write.

- [ ] **Step 8: Re-run the freshness, availability, and confirmation tests GREEN**

Run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/unit/application/test_calendar_proposals.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/integration/m2/test_calendar_availability_repository.py backend/tests/integration/m2/test_calendar_proposal_versions.py -q
~~~

Expected: PASS with independent freshness observation, fixed query count, minimal projections, no transaction during pure computation, correct large-event results, expected-version CAS, validated confirmation partitions, and operation-specific readiness.

- [ ] **Step 9: Write failing 0019, key-version, and CalendarEvent v2 AAD tests**

In `backend/tests/integration/db/test_migrations.py`, first freeze the 0019 migration-entry matrix:

- derive target bytes exactly as
  `b"ai_employee.restore_target.v1\0" + unsigned_decimal_system_identifier_ascii + b"\0" + exact_database_name_utf8`
  with no trailing NUL. Accept only SQL signed-int64 system identifiers; nonnegative values map directly, negative
  values add `2^64` in an overflow-safe numeric/host integer before canonical unsigned decimal serialization. Reject
  empty/>63-byte/NUL/non-UTF-8 names, out-of-int64 inputs, whitespace/leading-zero serialized IDs,
  zero/>uint64 results, normalization/case-fold/truncation, and freeze the synthetic vector system ID `72623859790382856`, database
  `ai_employee_restore_test`, digest
  `71f328c1e8eb5d9cd2f8e1f815afac4b723f94a274900853c895fd9d72e60e49`, lifecycle key
  `-9116408252019116299`, and target key `5971001870339114140`. When the target is absent, only the confirmed-name
  create/reset wrapper may reuse those exact bytes and must verify the new `pg_database.datname` afterward;
- independently freeze the high-bit vector SQL `-1` → uint64 `18446744073709551615`, same database name → digest
  `ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b`; expected bytes/digest must come from a
  standard-library test encoder, never the production helper;
- every online migration first acquires the target lifecycle advisory lock from management database `postgres`, then
  the target lock, and reads both database-wide `ai_employee.maintenance_gate` and
  `ai_employee.restore_call_authority` directly from `pg_db_role_setting` before any write. A held lifecycle/target
  lock, active or `needs_attention` restore after holder crash, malformed value, role-specific override, duplicate
  setting, or stale `current_setting` assumption leaves DDL/DML/version writes at zero. A new session must observe
  valid catalog settings through `current_setting(..., true)`. At the Task 16A boundary, the ordinary idle path
  requires gate reset and call authority absent because no restore executor exists yet; Task 27D later extends this
  same admission helper into the closed `pristine_idle | active | completed_idle` matrix. Pristine idle keeps
  gate/call/completion absent with baseline ACL and remains legal for migration/role-bootstrap. Completed idle requires
  a non-optional, zero-slot-digest-valid completed call plus matching `ai_employee.restore_completion`, gate absent, and
  baseline ACL; only the completion audit/local projection may already have been removed by ordinary retention,
  privacy deletion, or host loss;
- every online migration path uses the fixed session-level
  `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`. Hold `pg_advisory_lock_shared(20260806, 143)` on an independent
  connection, start fresh/0018 ordinary upgrades, and prove the Alembic outer boundary cannot commit or release its
  exclusive lock until that shared holder exits. After release, exactly one migration transaction commits; injected
  failure or connection loss releases only after rollback and never permits a later commit;
- a fresh database using the same ordinary `alembic upgrade head` path as Compose, E2E, and the integration
  session fixture succeeds without any rollout artifact because the built-in guard proves the affected set empty;
- a database at 0018 with a strictly empty affected set also succeeds through the same zero-bootstrap branch, and
  the guard repeats that proof before mutation and from `on_version_apply` before final commit;
- a database at 0018 with any historical complete AEAD triple and no injected guard fails before the first 0019
  DDL/DML statement, leaving the version and every row unchanged;
- an injected typed Fake guard receives exactly `before_mutation` and `before_commit` on the same migration
  transaction boundary. A Fake that rejects either phase leaves revision 0018 and rolls back all 0019 changes;
- a missing attribute, wrong object, wrong phase, stamp/downgrade/other revision, or callback invocation outside the
  exact online 0019 upgrade fails closed without changing ordinary migration behavior for other revisions.

Exercise the real entrypoints through `backend/tests/integration/conftest.py`,
`scripts/test-run-e2e-backend.sh`, `scripts/test-tooling.sh`, and `scripts/test-deployment.sh`: fresh
CI/E2E/Compose/`just db-upgrade` must remain green without rollout artifacts, while no entrypoint may set an
implicit allow flag for a non-empty affected database or invoke Alembic outside the common management/target/schema
lock boundary. Run `backend/tests/integration/operations/test_database_maintenance_gate.py` against the same Task13
database and use the fixed canonical target/lifecycle/target-lock/gate/call-authority vectors; do not derive
authority from a state filename.
The dedicated Task 27C `calendar_aad_migrate_0019` wrapper must inherit the same lock automatically rather than
declaring a second key or release rule.

Freeze the obsolete-database operator boundary in `scripts/test-tooling.sh`: no `repair-0019`, stamp, downgrade, or
revision-rewrite recipe may appear. The existing `just db-reset` remains restricted to explicit `development|test`,
localhost/exact database identity and exact typed confirmation, but must no longer contain/directly invoke
`docker compose exec ... dropdb` or `createdb`. It calls the typed lifecycle CLI, whose one long-lived management
session takes the target lifecycle lock from `postgres`, rechecks both catalog authorities, performs drop/create,
and invokes ordinary migration through a live in-process lease before releasing the lock. Simulate another-host
restore holder crash followed by reset: active gate/call authority must keep `drop_calls == create_calls ==
migration_calls == 0`. Production-like environment/target, lock contention, malformed authority or fake/env lease
must fail before any lifecycle call. This is the only permitted rebuild of a hand-applied pre-Task-16A experimental
0019; it does not create a migration branch.

Then upgrade a synthetic database to `20260809_0018`, inject the typed Fake guard, seed all of these facts, and
upgrade to `20260809_0019`:

- two connections that share the same `calendar_id`, where only the first connection has an event with a complete description AEAD triple and empty location triple;
- another event on the affected connection with complete description and location triples;
- exact Calendar event cursors for both same-ID connection/calendar pairs plus a distinct unaffected calendar;
- connected owning OAuth connections, enabled `calendar.read` capabilities, both access-token and refresh-token local AEAD credential rows, and exact user/connection-owned `ProviderCalendar` rows for every affected pair;
- a `directory` cursor with non-null cursor, `last_success_at`, and existing error state.

Assert 0019 preserves event IDs, calendar IDs, ciphertext, nonce, key version, timestamps, and every non-AAD business field byte-for-byte; sets version 1 only on complete historical triples; leaves fully empty groups at `NULL`; clears only the exact affected connection/calendar pair's non-directory `cursor` and `last_success_at`; writes only `calendar_event_resync_required` to its `last_error_code`; leaves the other connection's same-`calendar_id` cursor completely unchanged; and leaves directory cursor/freshness/revision/error values unchanged. Add separate preflight tests where either field has a partial historical triple, an affected `(connection_id, calendar_id)` lacks its exact non-directory cursor, the owning connection is disconnected, `calendar.read` is disabled or revoked, the access credential is missing, the refresh credential is missing/access-only, or the exact `ProviderCalendar` row is missing. Also cover inconsistent user ownership wherever the 0018 constraints permit constructing it. Each upgrade must fail before any DDL/DML, leave Alembic at `20260809_0018`, preserve every row and byte—including connection, capability, credential, directory, cursor, and event facts—and create no guessed marker. Decryptability/provider usability belongs to Task 27C's network preflight; the migration must not claim that row existence proves either.

After upgrade, assert each description/location group accepts only all-null or all-non-null with `aad_version IN (1, 2)`, rejects partial groups and versions outside 1/2, and keeps the two field versions independent. Assert `command.downgrade(..., "20260809_0018")` raises the migration's forward-only error and does not drop columns or constraints.

In Google and Microsoft sync integration tests, decrypt each newly synchronized description and location with exactly:

~~~python
aad = (
    f"{user_id}:{connection_id}:{calendar_id}:{provider_event_id}:{field}"
).encode("ascii")
assert aad_version == 2
assert cipher.decrypt(EncryptedValue(ciphertext, nonce, key_version), aad) == expected
~~~

Assert the old v1 AAD cannot decrypt the new value. For each provider, synchronize two calendars that share one `provider_event_id` and prove both rows remain independently decryptable only with their own calendar ID.

In `backend/tests/unit/security/test_encryption.py`, use the real single-key `AeadCipher`: assert its public
read-only `key_version` property returns the constructor version and cannot be assigned; encrypt with key version
7, tamper only `EncryptedValue.key_version`, and assert `decrypt()` raises the stable
`EncryptionKeyVersionError` before calling `AESGCM`. The implementation must compare the persisted version
exactly with the same public version exposed to composition roots; silently ignoring the mismatch or reading a
private `_key_version` is forbidden. A multi-key keyring is outside M2 Task 16A.

In `backend/tests/integration/m2/test_calendar_proposal_versions.py`, cover the public precise-event reader with all-null fields, v1 fields, a missing cipher, a v2 `InvalidTag`, typed key-version/decryption-boundary errors, invalid UTF-8, and a complete four-column ciphertext swap between two calendars sharing one provider event ID. All-null reads as `""`; unavailable cipher state, v1, unknown version, swapped ciphertext, and ciphertext/nonce/key-version/version tampering raise `StateConflictError(error_code="calendar_event_resync_required")`. Use a recording Fake cipher only to record attempted decryptions and assert there is no second attempt with v1 AAD, an omitted calendar ID, or unauthenticated plaintext; the Fake must not stand in for the real key-version tamper test.

- [ ] **Step 10: Run the 0019 and v2 AAD RED tests**

Run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/operations/test_database_maintenance_gate.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/security/test_encryption.py -q
bash scripts/test-run-e2e-backend.sh
bash scripts/test-tooling.sh
bash scripts/test-deployment.sh
~~~

Expected: FAIL because revision 0019, its frozen typed guard protocol, mutation/final-commit checks, and AAD-version
columns do not exist; ordinary upgrade entrypoints do not yet distinguish fresh/strict-zero bootstrap from an
affected 0018 database, derive the frozen target/lock vectors, obtain a management-database lifecycle lease, honor
durable maintenance/call authority, or serialize in management→target→schema order; `db-reset` still contains direct
unguarded drop/create commands and cannot hold one typed lease across check→drop→create→migration. Custom GUC
parsing/new-session visibility, active-authority zero lifecycle calls, and owner-entry zero-write proofs do not exist;
cursor invalidation is not pair-safe;
the migration does not yet fail closed on missing
cursor/disconnected connection/non-enabled capability/missing access or refresh credential/missing ProviderCalendar;
`AeadCipher` has no public read-only key-version contract and ignores persisted key-version mismatch; sync still
uses the legacy connection/event AAD; and the repository reader neither rejects a missing cipher nor maps only the
approved typed failures to `calendar_event_resync_required`.

- [ ] **Step 11: Add forward-only 0019, v2-only writes, and fail-closed reads**

Create `20260809_0019_calendar_event_field_aad_v2.py` with:

~~~python
revision = "20260809_0019"
down_revision = "20260809_0018"

_AAD_V1 = 1
_AAD_V2 = 2
_RESYNC_REQUIRED = "calendar_event_resync_required"
~~~

Create `application/ports/calendar_aad_migration_guard.py` as the final frozen boundary. It exposes a closed phase
union `before_mutation | before_commit`, a typed guard callable/object, runtime validation, and one resolver used by
both the revision and Alembic environment. If no guard is injected, the resolver may return only the built-in
zero-bootstrap guard, which runs the exact 0018 affected-set query and succeeds only when the set is empty. It must
not read an environment allow flag, accept an untyped truthy object, or infer safety from database age.

Create `infrastructure/db/database_maintenance.py` with the exact signed-bigint-to-uint64 conversion, target
bytes/boundaries, low/high-bit fixed synthetic digests, signed-big-endian lifecycle/target lock vectors, strict
gate/call-authority grammar, direct `pg_db_role_setting`
reads, role-override rejection, and typed lifecycle/owner admission entries; Task 27D later adds the completion-GUC
grammar plus the closed pristine/active/completed matrix and required completed-call/completion-GUC zero-slot pair
proof to these same entries without requiring a surviving audit row. Create
`cli/database_maintenance.py` so ordinary migration and `db-reset` share one implementation. The lifecycle context
opens a long-lived connection to management database `postgres`, acquires the target lifecycle key there, and
returns an in-process typed lease tied to that exact live session; it is not serializable and cannot be supplied by
environment/file. In the outer online Alembic path, acquire that lease, then target lock/check gate and call catalog admission,
then blocking `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` before starting the migration transaction. Run all revisions
plus `on_version_apply`, commit/rollback, and only then release in reverse order. Connection/lock loss fails the
migration.

Rewrite the existing `just db-reset` body to keep its current development/test, localhost, exact target and full-name
confirmation gates, but delegate lifecycle mutation to the CLI instead of direct `dropdb`/`createdb`. The CLI holds
one management lease while it rechecks gate/call catalog admission, performs exact quoted DROP/CREATE, and invokes ordinary
online Alembic with the same live typed lease. A nested migration must consume that object rather than reacquire and
deadlock; no public skip flag is allowed. Holder crash releases only the advisory lock, so a surviving active/
`needs_attention` catalog authority blocks a later reset before drop/create. `compose.yaml`, integration fixtures,
E2E bootstrap, Task 27C migration wrapper and `scripts/init-db-roles.sh` inherit the same boundary. Do not place locks
inside revision 0019, use transaction-level locks, change guard phases, or reuse `(20260809, 19)`.

At the first line of `upgrade()` before any DDL/DML, resolve the guard from Alembic
`Config.attributes["calendar_aad_0019_guard"]` and invoke `before_mutation`. In
`backend/migrations/env.py`, install an `on_version_apply` callback that activates only for the exact online
upgrade step whose `up_revision_id == "20260809_0019"`; after the revision operation and version-table write but
before the surrounding migration transaction commits, invoke the same resolved guard with `before_commit` using
the callback connection. Reject stamp, downgrade, another revision, wrong callback shape, or a guard instance that
changes identity between phases. Other revisions and ordinary migration configuration remain unchanged. A failure
in either phase rolls back the complete 0019 transaction, including the Alembic version update.

The zero-bootstrap guard allows fresh database, CI/E2E, Compose migration service, integration fixtures, and
`just db-upgrade` to keep using ordinary `upgrade head` when the exact affected set is empty. At revision 0018
with any complete historical field triple, absence of an injected typed guard fails before mutation. Task 27C later
constructs the artifact-backed implementation and injects it through this attribute; it must not edit the revision,
`migrations/env.py`, or this protocol.

After the mutation-phase guard succeeds, the upgrade must preflight both legacy triples for partial-null rows and
derive every affected `(connection_id, calendar_id)` plus its `user_id` from complete historical triples. For
every pair, prove all of these existing 0018 facts with exact user/connection/scope joins: the required non-directory
Calendar event cursor; an owning OAuth connection in `connected`; an `enabled` `calendar.read` capability; both
`access_token` and `refresh_token` AEAD credential rows required for the bounded rollout; and the exact
`ProviderCalendar` row. Access-only is an explicit migration failure. A missing or cross-user fact, disconnected
connection, or disabled/revoked/otherwise non-enabled capability must raise a stable migration error while revision
remains 0018 and every row/byte remains unchanged; never synthesize a cursor, credential, directory row, or guessed
marker. The migration does no provider network I/O and cannot prove refresh-token decryptability or provider
usability; Task 27C owns the separate active-refresh preflight and deadline artifact. After local preflight, add
nullable `description_aad_version` and `location_aad_version`; backfill 1 only where that field's legacy triple is
fully non-null; and create independent named checks equivalent to:

~~~sql
(
  description_ciphertext IS NULL
  AND description_nonce IS NULL
  AND description_key_version IS NULL
  AND description_aad_version IS NULL
)
OR
(
  description_ciphertext IS NOT NULL
  AND description_nonce IS NOT NULL
  AND description_key_version IS NOT NULL
  AND description_aad_version IN (1, 2)
)
~~~

Repeat the complete expression for location. For each preflighted affected pair, update only rows satisfying all of:

~~~sql
sync_cursors.connection_id = affected_connection_id
AND sync_cursors.scope_key = affected_calendar_id
AND sync_cursors.resource_kind = 'calendar'
AND sync_cursors.scope_key <> 'directory'
~~~

Set `cursor` and `last_success_at` to `NULL`, and set the content-free `last_error_code` to `calendar_event_resync_required`. Do not alter a different connection that reuses the same `calendar_id`, `last_attempt_at`, directory rows, event content, event identity, or AEAD bytes. `downgrade()` must raise a stable forward-only `RuntimeError` before issuing DDL or DML.

Mirror the columns and checks in `CalendarEventModel`. In `SyncCalendarUseCase`, build AAD as `user_id:connection_id:calendar_id:provider_event_id:field`; pass `event.calendar_id` explicitly; and have `SqlAlchemyCalendarSyncRepository.upsert_event()` set each field's version to 2 on insert and update.

In the application encryption port, add a public read-only `key_version: int` contract and define stable narrow
errors such as `EncryptionKeyVersionError` and `EncryptionBoundaryError`; do not use a broad catch-all error.
The single-key `AeadCipher` implements the property from its constructor-held version, exposes no setter, and
`decrypt()` compares `EncryptedValue.key_version` exactly with that same value before invoking `AESGCM`.
Composition roots in Tasks 27A–27C consume only this public accessor. Normalize only explicitly defined malformed
decryption-boundary conditions to `EncryptionBoundaryError`; do not hide programmer errors, add a multi-key
keyring, read `_key_version` outside the class, or silently select another key.

Replace the reader with an independent four-column policy per field:

~~~python
parts = (ciphertext, nonce, key_version, aad_version)
if all(part is None for part in parts):
    return ""
if any(part is None for part in parts) or aad_version != 2:
    raise _calendar_event_resync_required()
if self._field_cipher is None:
    raise _calendar_event_resync_required()
try:
    plaintext = self._field_cipher.decrypt(
        EncryptedValue(ciphertext, nonce, key_version),
        (
            f"{event.user_id}:{event.connection_id}:{event.calendar_id}:"
            f"{event.provider_event_id}:{field}"
        ).encode("ascii"),
    ).decode("utf-8")
except (
    InvalidTag,
    EncryptionKeyVersionError,
    EncryptionBoundaryError,
    UnicodeDecodeError,
) as error:
    raise _calendar_event_resync_required() from error
return plaintext
~~~

Unknown versions, unavailable cipher state, invalid key versions, explicitly typed decryption-boundary failures, invalid UTF-8, and authenticated-decryption failures must produce the same content-free resync error. The reader must not catch `AttributeError` or use `except Exception`; unexpected programming faults must remain visible. Never attempt v1 AAD, omit `calendar_id`, return partial plaintext, decrypt/re-encrypt inside the migration, or delete/merge an event.

- [ ] **Step 12: Run focused, adjacent, and full verification**

Focused migration/security run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/operations/test_database_maintenance_gate.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/security/test_encryption.py -q
bash scripts/test-run-e2e-backend.sh
bash scripts/test-tooling.sh
bash scripts/test-deployment.sh
~~~

Expected: PASS, including fresh/strict-zero ordinary `upgrade head`, affected-without-guard pre-mutation failure,
typed Fake guard calls at mutation and final-commit phases, final-guard rollback including the Alembic version row,
unchanged behavior for other revisions, management/target/gate-call catalog check before every migration write, active/crashed restore
gate/call authority and malformed/role-override zero-write rejection, fixed low/high-bit target conversion plus
lifecycle/target-lock vectors,
new-session GUC visibility, management→target→schema order, and fixed schema-lifecycle exclusive lock held through
outer transaction commit/rollback across CI/E2E/Compose/`db-upgrade`/dedicated 0019 entrypoints. The existing
`db-reset` must prove one live management lease covers check→drop→create→migration, contains no direct unguarded
`dropdb`/`createdb`, and performs zero lifecycle calls after a crashed restore leaves active authority; exact-pair
cursor invalidation;
pre-DDL rollback to 0018 for missing cursor, disconnected connection, disabled/revoked `calendar.read`, missing
access credential, missing refresh credential/access-only, or missing ProviderCalendar; public read-only
`AeadCipher.key_version`; real-cipher key-version tamper rejection; explicit missing-cipher failure; and no legacy
decrypt fallback.

Adjacent calendar/worker run:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/unit/application/test_calendar_proposals.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/integration/m2/test_calendar_availability_repository.py backend/tests/integration/workers/test_outbox_dispatch.py backend/tests/unit/workers/test_execution_lease.py backend/tests/contract/test_calendar_adapter.py backend/tests/contract/microsoft/test_calendar_adapter.py -q
~~~

Expected: PASS with time-valid renewal, one runner clock sample per renewal boundary, and only Fake/synthetic provider data.

Full repository gate:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test just check
~~~

Expected: PASS with fresh backend/frontend unit tests, lint, type checking, and no real-provider access. Then run `git diff --check` and inspect the complete diff for accidental Task 18 approval/ToolExecution/provider-write work, unbounded sensitive ORM loads, legacy AAD fallback, or any database URL other than the Task 13 synthetic test database.

These results prove implementation readiness only. They may verify the final frozen 0019 guard protocol, zero
bootstrap, marking, fail-closed reads, and bounded-sync behavior with synthetic data, but they must not be recorded
as completion of the non-rolling deployment or any production scope recovery. Task 27C must treat the revision and
`migrations/env.py` as immutable consumers of its injected guard.

- [ ] **Step 13: Commit**

~~~bash
git add compose.yaml backend/migrations/env.py backend/migrations/versions/20260809_0019_calendar_event_field_aad_v2.py backend/src/ai_employee/application/ports/calendar_aad_migration_guard.py backend/src/ai_employee/application/ports/encryption.py backend/src/ai_employee/infrastructure/db/database_maintenance.py backend/src/ai_employee/cli/database_maintenance.py backend/src/ai_employee/infrastructure/security/encryption.py backend/src/ai_employee/infrastructure/db/models/sources.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/application/use_cases/calendar_proposals.py backend/src/ai_employee/domain/calendar_availability.py backend/src/ai_employee/workers/prepare_calendar_restore.py backend/src/ai_employee/application/use_cases/task_execution.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/infrastructure/db/repositories/calendar_availability.py backend/src/ai_employee/infrastructure/db/repositories/task_execution.py backend/tests/integration/conftest.py backend/tests/integration/db/test_migrations.py backend/tests/integration/operations/test_database_maintenance_gate.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/unit/application/test_calendar_proposals.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/integration/m2/test_calendar_availability_repository.py backend/tests/integration/workers/test_outbox_dispatch.py backend/tests/unit/workers/test_execution_lease.py backend/tests/unit/security/test_encryption.py justfiles/db.just scripts/init-db-roles.sh scripts/run-e2e-backend.sh scripts/test-run-e2e-backend.sh scripts/test-tooling.sh scripts/test-deployment.sh
git commit -m "fix: harden calendar proposal trust boundaries"
~~~

### Task 17: Expose calendar proposal, restore, and working-settings APIs

**Files:**
- Create: `backend/src/ai_employee/api/routers/calendar.py`
- Modify: `backend/src/ai_employee/api/routers/settings.py`
- Modify: `backend/src/ai_employee/application/use_cases/calendar_proposals.py` — user-scoped restore-enqueue boundary and exact durable task input.
- Modify: `backend/src/ai_employee/application/use_cases/settings.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py` — minimal persistent restore-source projection and atomic TaskRun/Outbox creation.
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

Cover all eight proposal routes, creation replay under the same `Idempotency-Key`, version conflicts, restore ownership, an exact historical `snapshot_id` in the restore request, restore idempotency, 201/202 semantics, no-store responses, recurrence/read-only/ETag errors, CSRF, cross-user 404, all-day date schemas, IANA timezone validation, and overlapping working-hours rejection. Restore tests must call through the application use case and prove from persistent facts that `source_snapshot_id` belongs to the current user, `snapshot_kind = before`, its source proposal has `operation_kind = update` and `status = applied`, the snapshot target event exactly equals the path `event_id`, and the snapshot ciphertext is still retained. Missing/cross-user sources return 404; invalid lifecycle, kind, event binding, or expired ciphertext returns 409. Every invalid source creates neither a `TaskRun` nor an Outbox row. A valid source atomically creates both and persists the exact input key set `{"source_snapshot_id", "creation_idempotency_key"}`—with no `event_id`, `snapshot_id`, or extra key.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py -q`

Expected: FAIL because the calendar router and settings fields are absent.

- [ ] **Step 3: Implement strict calendar API schemas**

Create discriminated `CreateProposalRequest` variants for `create` and `update`, an optimistic `UpdateProposalRequest`, typed field-diff responses, candidate-time responses, `RestoreProposalRequest(snapshot_id: UUID)`, and `AcceptedTaskResponse`. Reject provider-specific recurrence, conference, attachment, and raw extension fields with 422 at Pydantic validation.

The candidate response requires `attendee_availability_checked=false`; no route or provider adapter in
M2 accepts attendee Free/Busy inputs.

`suggest-times` performs database reads and deterministic CPU work without creating a TaskRun; it must not keep a database transaction open while calculating candidates. `POST /calendar/proposals`, `submit`, and `restore-proposal` require `Idempotency-Key`; proposal creation returns the same 201 representation on replay, while the long actions return HTTP 202 with persistent task IDs.

For `POST /calendar/events/{event_id}/restore-proposal`, the router parses the path/request schema and calls an application use case; it must not query ORM models or create queue facts directly. Before enqueueing, the use case asks the user-scoped repository for a minimal persistent projection and verifies all of these facts in one application boundary: `source_snapshot_id` belongs to the current user; `snapshot_kind = before`; the source proposal has `operation_kind = update` and `status = applied`; the target event exactly matches the path `event_id`; and the encrypted snapshot group is still retained. Missing or cross-user facts map to 404, while an ineligible retained fact maps to 409. Either failure occurs before and outside Task creation, so no `TaskRun` or Outbox row may exist.

Only after those checks may the repository atomically create the durable `calendar.restore.prepare` TaskRun and Outbox. Its persisted input has exactly `{"source_snapshot_id", "creation_idempotency_key"}`, with no key named `event_id` or `snapshot_id` and no extra key. The Worker retains Task 16A's exact-key validation and rechecks the same ownership, before-snapshot, applied-update, exact-event, and retained-ciphertext invariants before any provider access. Task 17 must not create an ApprovalRequest, freeze a command, or otherwise implement Task 18 early.

- [ ] **Step 4: Extend settings responses and validation**

Add `default_mail_connection_id`, `default_calendar_connection_id`, `default_calendar_id`, fully normalized seven-day `working_hours`, and `meeting_buffer_minutes` to application/API types. The settings repository verifies defaults belong to the current user and have the required enabled capability; invalid defaults return `connection_capability_disabled` rather than silently falling back. Calendar routes map `idempotency_key_payload_mismatch` and `external_writes_disabled` to 409 with an actionable recovery response.

- [ ] **Step 5: Run API and domain regressions**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py backend/tests/unit/domain/test_calendar_availability.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/api/routers/calendar.py backend/src/ai_employee/api/routers/settings.py backend/src/ai_employee/application/use_cases/calendar_proposals.py backend/src/ai_employee/application/use_cases/settings.py backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py backend/src/ai_employee/infrastructure/db/repositories/settings.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py
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

HTTP tests must cover `POST /gmail/v1/users/me/messages/send`, Base64URL without padding requirements, `threadId` for replies, normalized returned message/thread IDs, Sent search by stable Message-ID, one coordinator-claimed 401 refresh (no adapter-local retry; unknown re-entry has zero provider calls), 400/403/429 confirmed rejection, connect failure before send, read timeout after send, ambiguous 5xx, and no Draft API call.

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

Cover Base64 MIME `text/plain` requests for `/me/sendMail`, `/me/messages/{id}/reply`, and `/replyAll`; the documented default save-to-Sent-Items behavior without an invalid JSON wrapper around MIME; `Prefer: IdType="ImmutableId"`; frozen recipient compatibility preflight; no Graph draft endpoint; 202 with empty body; Sent Items filter by stable `internetMessageId`; one coordinator-claimed 401 refresh (no adapter-local retry; unknown re-entry has zero provider calls); 403 scope; 429 confirmed rejection; connect failure; post-send timeout; 5xx unknown; and personal/work account fixtures.

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

### Task 27A: Close OAuth automatic-refresh and API access-log boundaries

**Depends on:** Task 16A's public read-only `AeadCipher.key_version` contract. This task does not touch revision
0019 or Alembic migration wiring.

**Files:**
- Modify: `compose.yaml`
- Modify: `compose.dev.yaml`
- Modify: `justfiles/dev.just`
- Modify: `scripts/run-e2e-backend.sh`
- Modify: `backend/src/ai_employee/infrastructure/observability/logging.py`
- Create: `backend/src/ai_employee/application/ports/oauth_refresh.py`
- Create: `backend/src/ai_employee/application/ports/credential_rotation.py`
- Create: `backend/src/ai_employee/application/oauth_refresh_identity.py`
- Create: `backend/src/ai_employee/application/calendar_aad_digests.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_mail.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/oauth_refresh_coordinator.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/credential_rotation.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/email.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/workers/sync_mail.py`
- Modify: `backend/src/ai_employee/workers/sync_calendar.py`
- Create: `backend/tests/unit/application/test_calendar_aad_digests.py`
- Create: `backend/tests/unit/application/test_oauth_refresh_identity.py`
- Create: `backend/tests/unit/application/test_oauth_refresh_coordinator.py`
- Create: `backend/tests/integration/m2/test_credential_rotation_repository.py`
- Create: `backend/tests/integration/m2/test_oauth_refresh_coordinator.py`
- Modify: `backend/tests/integration/google/test_gmail_sync.py`
- Modify: `backend/tests/integration/google/test_calendar_sync.py`
- Modify: `backend/tests/integration/microsoft/test_mail_sync.py`
- Modify: `backend/tests/integration/microsoft/test_calendar_sync.py`
- Create: `backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py`
- Modify: `backend/tests/unit/observability/test_redaction.py`
- Modify: `scripts/test-tooling.sh`
- Modify: `scripts/test-deployment.sh`

- [ ] **Step 1: Write failing coordinator, identity, CAS, and real-Uvicorn canary tests**

Freeze the three digest vectors and the fixed HKDF/HMAC identity vector independently from production helpers.
Write the automatic `calendar_aad_preflight | provider_refresh` claim tests for connection-scoped session lease,
committed `oauth.refresh_started` before network, complete generation/access/refresh snapshot CAS, atomic
`oauth.refresh_confirmed`, missing/same byte preservation, different-token confirmed without old-fence
consumption, and zero provider calls after lock loss, network unknown, CAS miss, definite rollback, Taskiq replay, or
`TransientProviderError`. Add fresh-session result-union reconcile tests for confirmed actual commit/rollback and
closure/current-readiness separation, including changed=false access-only/same-plaintext re-encryption and
changed=true A→B→A conflict.

Statically scan all four real API launch entries—`compose.yaml`, `compose.dev.yaml`,
`justfiles/dev.just`, and `scripts/run-e2e-backend.sh`—and require `--no-access-log` or equivalent
`access_log=False`. Start a real Uvicorn subprocess against a synthetic database, send an OAuth callback query
containing synthetic `code`, `state`, and `error_description`, scan stdout, stderr, and application JSON logs,
and assert all three values have zero matches. Query PostgreSQL and assert the callback-error path still consumed
state once and persisted only the stable sanitized audit/error facts. Also reject raw URL/query/request-target keys
from the structured logging schema. A router/filter-only test is insufficient.

- [ ] **Step 2: Run the focused RED gate**

~~~bash
uv run --project backend pytest \
  backend/tests/unit/application/test_calendar_aad_digests.py \
  backend/tests/unit/application/test_oauth_refresh_identity.py \
  backend/tests/unit/application/test_oauth_refresh_coordinator.py \
  backend/tests/unit/observability/test_redaction.py -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/m2/test_credential_rotation_repository.py \
  backend/tests/integration/m2/test_oauth_refresh_coordinator.py \
  backend/tests/integration/google/test_gmail_sync.py \
  backend/tests/integration/google/test_calendar_sync.py \
  backend/tests/integration/microsoft/test_mail_sync.py \
  backend/tests/integration/microsoft/test_calendar_sync.py \
  backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py -q
bash scripts/test-tooling.sh
bash scripts/test-deployment.sh
~~~

Expected: FAIL because the shared coordinator/rotation ports, fixed-root identity, strict result parser, zero-call
re-entry path, four-entry access-log shutdown, structured-log URL exclusion, and real subprocess canary do not exist.

- [ ] **Step 3: Implement the automatic coordinator and close every access-log entry**

Implement the existing-AuditEvent two-channel result types, but in this task expose only automatic claim/result and
the shared explicit-recovery lease primitive used by Task 27B. Construct AEAD and identity services from the same
`APP_MASTER_KEY_FILE` bytes and Task 16A's public `cipher.key_version`; never read `_key_version`, add a second
Secret, or introduce a keyring. Make the coordinator the only existing-connection automatic provider-call admission
and the credential-rotation repository the only writer. Preserve all detailed fence, G/G/G, digest,
disposition/flag/equality, ACK-loss, current-readiness, lock-order, and no-replay rules in the reference section
below.

Disable Uvicorn access logs in all four launch entries and keep application JSON logs on the existing stable-field
allowlist with no raw URL/query/request target. Do not suppress the sanitized application audit needed to diagnose a
callback error.

- [ ] **Step 4: Verify and commit Task 27A**

Re-run the Step 2 commands, then:

~~~bash
uv run --project backend ruff check backend/src backend/tests
uv run --project backend mypy backend/src
git diff --check
git add compose.yaml compose.dev.yaml justfiles/dev.just scripts/run-e2e-backend.sh scripts/test-tooling.sh scripts/test-deployment.sh backend/src/ai_employee/infrastructure/observability/logging.py backend/src/ai_employee/application/ports/oauth_refresh.py backend/src/ai_employee/application/ports/credential_rotation.py backend/src/ai_employee/application/oauth_refresh_identity.py backend/src/ai_employee/application/calendar_aad_digests.py backend/src/ai_employee/application/use_cases/sync_mail.py backend/src/ai_employee/infrastructure/db/repositories/oauth_refresh_coordinator.py backend/src/ai_employee/infrastructure/db/repositories/credential_rotation.py backend/src/ai_employee/infrastructure/db/repositories/email.py backend/src/ai_employee/integrations/registry.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/workers/sync_mail.py backend/src/ai_employee/workers/sync_calendar.py backend/tests/unit/application/test_calendar_aad_digests.py backend/tests/unit/application/test_oauth_refresh_identity.py backend/tests/unit/application/test_oauth_refresh_coordinator.py backend/tests/integration/m2/test_credential_rotation_repository.py backend/tests/integration/m2/test_oauth_refresh_coordinator.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_mail_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py backend/tests/unit/observability/test_redaction.py
git commit -m "fix: harden oauth refresh coordination"
~~~

### Task 27B: Add progressive recovery and the versioned callback result union

**Depends on:** Task 27A. It reuses the same connection lease, snapshot CAS, identity service, and strict result
parser; it does not add another automatic started event or credential writer.

**Files:**
- Modify: `backend/src/ai_employee/application/ports/oauth_refresh.py`
- Modify: `backend/src/ai_employee/application/ports/credential_rotation.py`
- Modify: `backend/src/ai_employee/application/use_cases/connections.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/connections.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/credential_rotation.py`
- Modify: `backend/src/ai_employee/api/routers/connections.py`
- Modify: `backend/tests/unit/application/test_oauth_refresh_coordinator.py`
- Modify: `backend/tests/unit/application/test_connection_capability_use_cases.py`
- Modify: `backend/tests/integration/m2/test_connection_capability_repository.py`
- Modify: `backend/tests/integration/m2/test_credential_rotation_repository.py`
- Modify: `backend/tests/integration/m2/test_oauth_refresh_coordinator.py`
- Modify: `backend/tests/integration/api/test_connections.py`
- Modify: `backend/tests/integration/google/test_oauth_flow.py`
- Modify: `backend/tests/integration/microsoft/test_oauth_flow.py`

- [ ] **Step 1: Write failing F/S/T, unsatisfied/replacement, and callback-shape tests**

Cover repeated connection-bound OAuthAttempts, atomic recovery-started creation with the existing single S→T
increment, one-time code exchange under the shared lease, target-`T` anti-replay, controlled-memory
`compare_digest`, denial/missing/same/known-failure convergence from `authorizing` to `action_required`,
stale-`T` no-op, and a later different-token attempt that atomically writes old != new, changed=true replacement
proof. Test actual commit/rollback ACK loss for unsatisfied and replacement, and assert only replacement consumes the
original automatic fence.

For Google and Microsoft callbacks, require exactly one of `code+state` or bounded non-empty `error+state`;
shape errors reject before state consumption, every valid error consumes state once, replay fails, raw provider
values never persist/log, and the stable classification matrix preserves
`microsoft_admin_consent_required`, `microsoft_reauthorization_required`, and
`oauth_authorization_failed`. A targetless identity that resolves to a fenced connection must fail before any
credential/scope/capability save.

- [ ] **Step 2: Run the progressive-recovery RED gate**

~~~bash
uv run --project backend pytest \
  backend/tests/unit/application/test_oauth_refresh_coordinator.py \
  backend/tests/unit/application/test_connection_capability_use_cases.py -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/m2/test_connection_capability_repository.py \
  backend/tests/integration/m2/test_credential_rotation_repository.py \
  backend/tests/integration/m2/test_oauth_refresh_coordinator.py \
  backend/tests/integration/api/test_connections.py \
  backend/tests/integration/google/test_oauth_flow.py \
  backend/tests/integration/microsoft/test_oauth_flow.py -q
~~~

Expected: FAIL because recovery-started/unsatisfied/replacement metadata, capability convergence, targetless
pre-save blocking, provider-error consumption/classification, and result-union ACK reconciliation are absent.

- [ ] **Step 3: Implement progressive recovery without weakening the fence**

Extend the existing progressive authorization flow only. Preserve the full F/S/T fields, strict timestamps,
requested-capability transition, no credential post/expiry on unsatisfied, old != new/changed=true replacement,
actual-commit/rollback reconcile, current-readiness separation, disconnect warning, and targetless blocking described
below. No path may call `ensure_connection`, perform an unconditional upsert, replay a code/provider call, or create
an OAuth-only service.

- [ ] **Step 4: Verify and commit Task 27B**

Re-run Step 2, run `git diff --check`, then:

~~~bash
git add backend/src/ai_employee/application/ports/oauth_refresh.py backend/src/ai_employee/application/ports/credential_rotation.py backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/infrastructure/db/repositories/connections.py backend/src/ai_employee/infrastructure/db/repositories/credential_rotation.py backend/src/ai_employee/api/routers/connections.py backend/tests/unit/application/test_oauth_refresh_coordinator.py backend/tests/unit/application/test_connection_capability_use_cases.py backend/tests/integration/m2/test_connection_capability_repository.py backend/tests/integration/m2/test_credential_rotation_repository.py backend/tests/integration/m2/test_oauth_refresh_coordinator.py backend/tests/integration/api/test_connections.py backend/tests/integration/google/test_oauth_flow.py backend/tests/integration/microsoft/test_oauth_flow.py
git commit -m "feat: add oauth refresh recovery"
~~~

### Task 27C: Implement 0019 preflight, real guard injection, and exact-pair resync

**Depends on:** Tasks 16A, 27A, and 27B. The Task 16A revision, migration-guard port, and
`backend/migrations/env.py` are read-only inputs to this task. This task must not modify
`backend/migrations/versions/20260809_0019_calendar_event_field_aad_v2.py`,
`backend/migrations/env.py`, or `backend/src/ai_employee/application/ports/calendar_aad_migration_guard.py`.

**Files:**
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_preflight.py`
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_rollout.py`
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_recovery.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_preflight.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_recovery.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Create: `backend/src/ai_employee/cli/calendar_aad_preflight_0019.py`
- Create: `backend/src/ai_employee/cli/calendar_aad_migrate_0019.py`
- Create: `backend/src/ai_employee/cli/calendar_aad_0019.py`
- Modify: `backend/src/ai_employee/workers/sync_calendar.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `justfiles/ops.just`
- Modify: `scripts/backup-postgres.sh` — add only the Task 27C rollout-guard/basename hook used by the sealed change
  window; Task 27D owns the generic versioned manifest, group publication, remote/retention, and restore tooling.
- Modify: `scripts/test-tooling.sh`
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_preflight.py`
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_recovery.py`
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py`

- [ ] **Step 1: Write failing preflight, injected-guard, deadline-tightening, and recovery tests**

Use the 0018 synthetic database and Fake/HTTP-mocked readers to cover the revision-global lease, exact local
recoverability, one proactive coordinator refresh per distinct connection, pair-only `initial_pages(scope_key)`,
content-free artifact, immutable-image binding, and zero Calendar mutation. Inject the real artifact-backed guard
through Task 16A's frozen Alembic attribute and prove both mutation and final-commit phases execute; do not edit the
revision to make the test pass.

Deadline tests must treat historical `oauth.refresh_confirmed.rollout_deadline_candidate` only as result-schema
proof. After every ACK-lost closure/current-readiness result and at every later guard, reload each affected
connection's current persisted access credential expiry, compute
`current_deadline = min(current_expiry) - 900 seconds`, and use
`effective_deadline = min(original_artifact_deadline, current_deadline)`. Add the explicit race where ACK is lost,
a later legal refresh/reauth persists a shorter expiry, and the rollout immediately tightens; a longer expiry may
never extend the artifact deadline.

Recovery tests freeze exact marker selection, ordinal reuse/allocation, exact-task in-process dispatch, bounded
event-scope reads, v2-only writes, marker-aware final CAS, zero directory/general-queue/provider-write calls, and
guard checks at start/provider/final commit.

- [ ] **Step 2: Run the 0019 RED gate**

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/operations/test_calendar_aad_0019_preflight.py \
  backend/tests/integration/operations/test_calendar_aad_0019_recovery.py \
  backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py -q
bash scripts/test-tooling.sh
~~~

Expected: FAIL because active-refresh preflight, artifact-backed implementation of the frozen guard, current-expiry
deadline tightening, no-argument migration composition, marker-only ordinal recovery, and exact-task runner are
absent.

- [ ] **Step 3: Implement the real rollout composition and exact recovery**

Implement the detailed revision-global lease, digest/artifact, provider probe, current-readiness, effective-deadline,
typed zero state, image binding, guard injection, planner/ordinal, exact dispatch, and marker-CAS protocols below.
`calendar_aad_migrate_0019.py` may construct a concrete guard and place it in Alembic `Config.attributes`; it
must not duplicate or change Task 16A's guard resolver. Add a diff assertion to the focused gate that the migration
revision, `migrations/env.py`, and migration-guard port remain unchanged in this commit.

- [ ] **Step 4: Verify and commit Task 27C**

Re-run Step 2, then:

~~~bash
git diff --check
git diff --exit-code HEAD -- backend/migrations/env.py backend/migrations/versions/20260809_0019_calendar_event_field_aad_v2.py backend/src/ai_employee/application/ports/calendar_aad_migration_guard.py
git add backend/src/ai_employee/application/use_cases/calendar_aad_preflight.py backend/src/ai_employee/application/use_cases/calendar_aad_rollout.py backend/src/ai_employee/application/use_cases/calendar_aad_recovery.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_preflight.py backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_recovery.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/cli/calendar_aad_preflight_0019.py backend/src/ai_employee/cli/calendar_aad_migrate_0019.py backend/src/ai_employee/cli/calendar_aad_0019.py backend/src/ai_employee/workers/sync_calendar.py backend/src/ai_employee/workers/execute_task.py justfiles/ops.just scripts/backup-postgres.sh scripts/test-tooling.sh backend/tests/integration/operations/test_calendar_aad_0019_preflight.py backend/tests/integration/operations/test_calendar_aad_0019_recovery.py backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py
git commit -m "feat: add guarded calendar aad rollout"
~~~

### Task 27D: Publish generic backup manifests; separate generic restore, sealed 0018 restore, and audit tooling

**Depends on:** Task 27C. This task owns generic backup-manifest/restore/audit tooling, extends Task 16A's common
owner-write admission helper, and preserves distinct generic/sealed/legacy validators. Only generic/sealed share the
crash-safe catalog admission and manifest-bound restore executor; legacy remains a disposable-target conversion
lifecycle with no workspace catalog mutation. It must not move retention/privacy behavior from Task 27E into this task.

**Files:**
- Modify: `backend/Dockerfile` — COPY generic/sealed/legacy/audit/maintenance-guard scripts and CLI modules from the
  repository-root build context into the immutable backend image; no host executable bind mount.
- Modify: `compose.yaml` — add operations-profile generic/sealed owner maintenance boundaries plus isolated legacy
  PostgreSQL/conversion services; generic/sealed use the image-internal shared restore executor, while legacy uses
  only its disposable-target registry/scavenger lifecycle and does not write workspace restore catalog facts; mount
  the state volume separately. The
  controller reads the long-lived bootstrap owner Secret, passes target connection credentials only to one psql
  consumer, and launches pg_restore with all `PG*`/DSN/Secret values removed.
- Modify: `.env.example` — add the development/test host default `RESTORE_STATE_DIR=./var/restore-state`, document
  the fixed container mount path, and require production to override it with an explicit dedicated absolute
  directory/volume satisfying owner/mode/no-symlink/non-overlap checks; no password or real Secret is stored here.
- Modify: `backend/src/ai_employee/infrastructure/db/database_maintenance.py` — add strict matching restore claim,
  `restore-call:v4` length/ASCII/parser/CAS rules, three-state admission, exact psql backend registration/reconcile,
  commit-time audit validation, and completed-call/completion-GUC/gate/ACL admission/ACK checks, while preserving
  Task 16A's management lifecycle lease and target vectors.
- Modify: `backend/src/ai_employee/cli/database_maintenance.py` — extend Task 16A's lifecycle wrapper into the one
  owner-session coordinator for active-attempt completion RESET plus gate establishment, catalog-authoritative call
  transitions/reconcile, fenced grants, role-switched verifier callbacks, and completion-GUC atomic reopen.
- Create: `backend/src/ai_employee/infrastructure/db/postgres_restore_stream.py` — Python process/pipe executor that
  supervises the credential-bearing psql consumer and credential-free pg_restore SQL generator, registers exact
  backend PID/start, enforces SQL-byte-zero before started, and captures both exits without a shell pipeline.
- Create: `backend/src/ai_employee/cli/postgres_restore.py` — image-owned generic/sealed composition CLI that binds
  validated manifest input, maintenance coordinator, stream executor, operation-specific verifier, and stable result.
- Create: `backend/tests/unit/infrastructure/db/test_postgres_restore_stream.py` — deterministic pipe/process tests
  for spawn-before-visible, backend-ready-before-feed, completion trailer, EOF rollback and child cleanup.
- Modify: `scripts/backup-postgres.sh` — serialize backup/publication/cleanup, bind dump revision under the shared
  schema-lifecycle lock, and publish/retain/copy an atomic manifest-bound three-file backup group.
- Modify: `scripts/restore-postgres.sh` — generic manifest-bound owner-role disaster restore only, including repeated
  no-business-writer checks, read-only backup mount, independent `RESTORE_STATE_DIR`, shared maintenance executor,
  catalog-authoritative attempt/reconcile, and no sealed validation logic.
- Modify: `scripts/init-db-roles.sh` — ordinary standalone bootstrap acquires management/target admission and checks
  gate/call/completion catalog facts, refusing active/`needs_attention` restore; expose reusable grant SQL/functions for the
  already-held restore executor without opening a second owner session or granting CONNECT early.
- Create: `backend/src/ai_employee/cli/verify_restored_backup.py` — manifest-revision-selected verifier functions
  run on the already established owner maintenance session after `SET ROLE ai_employee_app` and `BEGIN READ ONLY`.
- Create: `scripts/convert-legacy-backup.sh` — development/test-only disposable Compose conversion from a verified
  pre-manifest dump to a new ordinary manifest-bound group, with pre-resource durable registry/labels.
- Create: `scripts/scavenge-legacy-backups.sh` — fixed-grace registry/label/attempt-lock orphan reclamation after
  SIGKILL, host crash, or Docker daemon crash.
- Create: `scripts/test-legacy-backup-conversion.sh` — isolated lifecycle, SIGKILL/daemon-crash scavenging, active-run
  preservation, conflict/target isolation, and forbidden-output contract test.
- Create: `scripts/restore-calendar-aad-0018.sh` — sealed-window artifact/image-bound restore only.
- Create: `backend/src/ai_employee/cli/calendar_aad_verify_restored_0018.py` — sealed verifier callable imported by
  the existing holder executor; no standalone recipe, service, connection, or post-reopen invocation.
- Create: `scripts/audit-calendar-aad-0019.sh`
- Create: `scripts/test-calendar-aad-0019-audit.sh`
- Modify: `justfiles/ops.just` — resolve immutable backup image metadata, enforce serialized group conflicts and
  production maintenance-gate confirmations, orchestrate shared-executor generic/sealed restore, and expose exact
  `restore-legacy-to-isolated file output_basename` plus `legacy-backup-scavenge`.
- Modify: `justfiles/db.just` — preserve Task 16A's lifecycle-wrapped reset and add active restore-call-authority/
  completion-GUC holder-crash regression coverage without reintroducing direct drop/create commands.
- Create: `scripts/test-m2-release.sh` — initial release wrapper with an explicit audit invocation; Task 30 expands
  the remaining release matrix.
- Modify: `scripts/test-tooling.sh` — assert `.env.example`/Compose development default versus production-explicit
  restore-state configuration, backup/artifacts read-only, state separately writable with safe modes/non-overlap,
  and reset/drop/create cannot bypass the management lifecycle/catalog authority boundary.
- Modify: `scripts/test-deployment.sh`
- Modify: `backend/tests/unit/test_init_db_roles_script.py`
- Modify: `backend/tests/integration/retention/test_role_permissions.py`
- Create: `backend/tests/integration/operations/test_postgres_backup_restore.py` — backup/migration/cleanup races,
  management/reset/target lock races, signed-system-ID vectors, gate/call/completion catalog facts,
  `.env.example`/state-v4, database-zero-write already-applied, psql backend registration plus streamed SQL rollback,
  ordinal reconcile, prior-pair archive, no pre-restore gate audit, completed-call/completion-GUC/gate/ACL-authoritative
  atomic reopen with exact completion-audit insertion, and
  isolated legacy conversion.
- Modify: `backend/tests/integration/operations/test_database_maintenance_gate.py` — matching restore kind/source
  claims, the complete pristine/active/completed/next-active admission matrix, pristine migration/role-bootstrap,
  malformed/duplicate/off-matrix fail-closed cases, post-crash gate blocking, and all owner-entry zero-write coverage.
- Modify: `backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py`
- Modify: `docs/operations.md` — generic manifest/restore and sealed restore target contracts, without claiming the
  not-yet-executed production procedures are already available or completed.

- [ ] **Step 1: Write failing manifest, generic/sealed separation, stopped-write, and explicit-audit tests**

Freeze every new ordinary backup as adjacent `${dump}`, `${dump}.sha256`, and `${dump}.manifest.json`. The versioned
manifest schema is `ai_employee.postgres_backup_manifest.v1` and contains only a safe dump basename, strict UTC
`created_at`, PostgreSQL server version, Alembic revision, encrypted dump lowercase SHA-256/size, exact checksum
filename, the resolved immutable backend image `sha256:` content ID, and allowlisted content-free build/release
metadata, the deterministic content-free `restore_fingerprint_v1`, and an allowlisted digest map for every
image-internal generic/sealed/legacy/audit/maintenance executable. Assert all three files are `0600` and contain no
DSN, credential, token, body, calendar content, or personal data. The checksum must cover the encrypted dump and
finalized manifest by safe relative basename; the manifest must bind the exact checksum filename and repeat dump
digest/size. Any extra checksum line, symlink, absolute/unsafe name, digest/size/revision/fingerprint/image/script
mismatch, partial file, or collision fails closed.

Build the actual `backend/Dockerfile` from the repository-root context in the RED/GREEN contract. The resulting
immutable image must contain fixed-path copies of `backup-postgres.sh`, `restore-postgres.sh`,
`restore-calendar-aad-0018.sh`, `init-db-roles.sh`, `convert-legacy-backup.sh`,
`scavenge-legacy-backups.sh`, `audit-calendar-aad-0019.sh`, the generic/sealed verifier modules, and the maintenance
coordinator plus `ai_employee.cli.postgres_restore`/`postgres_restore_stream` modules. Rendered Compose/recipes may mount backup/artifact/Secret data read-only but must never bind mount an
executable over those paths. Missing files, digest mismatch, moved image, or executable bind mounts must fail before
owner Secret reads/connections and before any `pg_restore` call.

Before either backup serialization domain, every backup/restore/migration/reset lifecycle wrapper must acquire the
Task 16A `database_lifecycle_lock_key_v1` from management database `postgres`; restore holds it through the complete
attempt/reconcile/reopen, and reset holds it through check→drop→create→migration. Freeze the global order as
management lifecycle → target → schema. Simulate a restore holder crash and a reset from another host: after the
lifecycle session lock becomes available, durable active/`needs_attention` gate/call facts plus the required absence
of completion GUC must still yield zero
drop/create/migration calls. `justfiles/db.just` and `scripts/test-tooling.sh` must prove no direct `dropdb`/`createdb`
or environment lease bypass reappears.

Freeze `.env.example` and rendered Compose behavior for restore state: development/test uses the repository-ignored
host default `./var/restore-state`, mounted read-write only at container
`/var/lib/ai-employee/restore-state`; production must reject the relative default and require an explicit dedicated
absolute host directory/volume. Tests require directory `0700`, files `0600`, exact owner, no symlink, no
group/world-write, and resolved non-overlap with every read-only backup/artifact source. State contains only
content-free projection; database credentials, generated SQL, and other Secrets are forbidden, and no generated state is committed.

Freeze two additional independent backup serialization domains. Every backup/remote/retention/orphan run for the same target database
must hold session-level `BACKUP_LIFECYCLE_LOCK=(20260806, 274)` by executing
`pg_advisory_lock(20260806, 274)`; every run touching the same `${BACKUP_DIR}` must
also hold an exclusive `flock` on `${BACKUP_DIR}/.ai-employee-backup.lock`. Acquire in that fixed order and hold both
from unique `.partial.<uuid>` staging creation and final-name conflict check through dump/checksum/manifest
publication, remote upload, daily/weekly retention, and orphan cleanup. Same-database callers on different hosts
must serialize through PostgreSQL even when their local directories differ; same-host/same-directory callers for
the same or different database must serialize through `flock`.

The dump/revision binding uses the Task 16A lock separately. On the backup lock-holder session, acquire shared
`SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`, read the pre-Alembic revision, keep the shared lock across the complete
`pg_dump`, read the post-revision, and require exact equality plus proof that the same session still owns the lock.
Only then may the run publish the local final manifest and release the shared lock. All migration entrypoints hold
the matching exclusive lock through transaction commit, so a migration may wait but cannot commit during the dump.
No common MVCC snapshot between the revision reads and `pg_dump` is required; shared/exclusive locking plus the
pre/post CAS are both mandatory. A bypassed migration, changed revision, lost lock/session, or missing lock proof
must leave all three final names unpublished.

Test unique same-directory staging, no-clobber and manifest-last atomic publication: any dump/manifest/checksum
generation or rename failure cleans only the current run's staging and final members it can prove it created, while
a final manifest is the only publication marker. It must never delete or overwrite another run's conflicting member.
rclone uses a unique staging prefix per run, copies dump/checksum, and publishes the remote manifest last; backup
success is withheld if the configured remote group is partial or conflicts. Local and remote orphan grace is fixed
at 3600 seconds. While both serialization locks are held, cleanup must re-read mtime and final-manifest existence
immediately before deletion and may remove only an expired `.partial.<uuid>` tree or expired incomplete basename
group with no final manifest. Cover same/different-basename concurrency, same/different-host database locking,
cleanup racing a live run, crash immediately before manifest publication, and remote conflict; no case may remove
another run or a complete/recent group.

For generic `just restore file`, require the complete manifest-bound group to pass pure-file/schema/digest/size/
revision/image-metadata/`restore_fingerprint_v1`/image-internal-executable-digest verification before any owner
Secret, owner service, owner connection, or `pg_restore`.
Production must additionally resolve all three write switches to `false`, prove Caddy/API/Worker/Scheduler,
migration/role-bootstrap, backup/retention jobs, every general consumer/write-credential one-off, and all owner
maintenance/general consumers are stopped, then obtain the production-only second exact confirmation. Parameterize
every running-service, enabled-switch, missing-confirmation, manifest mismatch, and host/container race case and
assert `pg_restore_calls == 0`. Development/test must reject a write-capable service in the same workspace unless the
target is a separately proven isolated PostgreSQL instance.

After the final host/container state check, every repository-owned lifecycle/owner entry calls Task 16A's helper.
Reassert the low/high-bit target digest and signed lifecycle/target lock-key vectors from Task 16A; no script may reinterpret
system identifier/database bytes. The SQL `bigint` system identifier accepts only signed-int64 input; nonnegative
values serialize directly, negative values add `2^64` with no overflow before unsigned decimal ASCII serialization.
Freeze SQL `-1` → uint64 `18446744073709551615` with database `ai_employee_restore_test` → target digest
`ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b`, independently computed outside the
production helper. Restore first holds the management lifecycle session from `postgres`, then
nonblockingly acquires the target lock on the control owner session and keeps the required lock set through psql
backend registration, pg_restore-to-psql stream execution/reconcile, grants, verifier, and completion-GUC reopen.
Read `ai_employee.maintenance_gate`, `ai_employee.restore_call_authority`, and
`ai_employee.restore_completion` directly from `pg_db_role_setting`, accepting only one current-database,
`setrole=0` entry per key. Reject role override, duplicate/malformed rows, stale `current_setting`, invalid phase/
ordinal/backend/completion facts, and all nonmatching attempts.

The gate remains
`restore:v1:<attempt_uuid>:<generic|sealed_0018>:<target_digest>:<source_digest>` and is exactly 185 ASCII bytes for
generic or 189 for sealed. Generic/sealed require exact call authority
`restore-call:v4|<attempt_uuid>|<generic|sealed_0018>|<target_digest>|<source_digest>|<manifest_digest>|<previous_completion_digest_or_dash>|<call_ordinal>|<reopen_ordinal>|<phase>|<gate_established_at>|<call_started_at_or_dash>|<backend_pid_or_dash>|<backend_start_or_dash>|<pre_revision_or_dash>|<pre_fingerprint_or_dash>|<expected_revision>|<expected_fingerprint>|<observed_revision_or_dash>|<observed_fingerprint_or_dash>|<completed_at_or_dash>|<completion_authority_digest_or_dash>`.
Freeze ASCII-only encoding, exactly 21 pipe delimiters, maximum 1133 bytes, no empty/escaped/extra field, and the
phase-dependent parser/presence rules from the spec. Completion is exactly 86 ASCII bytes as
`restore_completion:v1:<64-lowercase-hex-digest>`. Direct `pg_db_role_setting` parsing requires exactly one
current-database/`setrole=0` occurrence for each present key; duplicate key/row, role override, malformed array,
unknown version/encoding, or last-write-wins interpretation fails closed. Writers use only lock-held
`ALTER DATABASE SET/RESET` and re-read the catalog result.

Freeze field monotonicity within one `call_ordinal`, not across real-call ordinal boundaries. Call-start and pre
facts frozen at starting persist throughout that ordinal; starting→ready may fill the backend pair once, after which
ready/started/outcome and successors preserve it; exact-post may fill the observed pair once, after which successors
preserve it. No populated fact may be cleared or rewritten within the ordinal.
The sole controlled boundary exception is a legal
`gate_established|restore_not_applied → restore_backend_starting` CAS: increment and never reuse the ordinal, write
a fresh database `call_started_at`, reset backend PID/start to `-`, re-freeze pre revision/fingerprint from the exact
current state just proven by the holder, and set observed/completed fields to the starting shape. Never inherit an
older ordinal's backend facts. Keep the direct exact-post ordinal-0 shape independent with call-start/backend/pre
all `-`.

Freeze the complete admission matrix and test every row and every off-matrix tuple:

| State | Gate | Call | Completion | ACL | Allowed caller |
|---|---|---|---|---|---|
| `pristine_idle` | absent | absent | absent | baseline | migration/role-bootstrap/ordinary lifecycle or new restore |
| `active` | present | same-attempt valid non-completed call | absent | PUBLIC/app/retention CONNECT revoked | matching generic/sealed executor only |
| `completed_idle` | absent | required valid completed call | present and zero-slot-digest-bound to call | baseline | ordinary lifecycle or archived-pair next restore |

Baseline ACL keeps PUBLIC revoked and only minimal app/retention CONNECT. Gate-only, call-only, completion-only,
missing completed call, digest/cross-attempt mismatch, active gate plus completion or baseline ACL, completed call plus
absent completion, and gate reset plus revoked ACL all fail closed. Do not reject `completion absent + baseline ACL`
by itself: that is valid pristine idle and must allow first-install migration/role-bootstrap. New sessions see all
three catalog facts, and migration/reset/drop/create/role-bootstrap/init-db-roles/generic/sealed/owner one-off paths
have zero lifecycle/write/call-authority/`pg_restore` activity under held locks, malformed authority, or a crashed
holder's active/`needs_attention` state. Legacy conversion uses only its disposable target, durable registry/scavenger,
and ordinary backup lifecycle; it never writes these workspace/production restore catalog facts.

Before a new generic/sealed request with no active gate establishes the gate or revokes CONNECT, the holder must use
the manifest-selected read-only verifier under management lifecycle + target lock to compare current revision and
the complete `restore_fingerprint_v1` with expected post. Exact equality returns evidenced
`restore_already_applied`, leaves database writes, audit, gate/call/completion mutation, psql, and pg_restore all at
zero, and preserves any prior completed authority. It publishes only deterministic local evidence at
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>.<kind>.<source_binding_digest_v1>.already-applied.json` with schema
`ai_employee.postgres_restore_already_applied.v1`, exact expected/observed revision and fingerprint, no timestamp or
random ID, and target/kind/source canonical bytes matching the filename. Publication uses a same-directory mode-`0600`
temp, file/directory fsync, and no-clobber create: an existing byte-identical file is idempotent, while an existing
different file fails closed without overwrite. Generic and sealed evidence therefore cannot collide. Any missing/
mismatched evidence continues to normal gate admission. A matching existing `gate_established` attempt may CAS directly to
`restore_succeeded` only after the same exact post proof; it starts no psql/pg_restore and writes no separate
already-applied audit, but still proceeds through grants, verifier, and the unified completion transaction.

A new generic/sealed attempt starts only from `pristine_idle` or `completed_idle`, freezing its new attempt UUID,
`call_ordinal=0`, `reopen_ordinal=0`, database `gate_established_at`, and exact identity facts. Pristine sets
`previous_completion_digest=-`. Completed idle first reconstructs and verifies the exact old completed call +
completion GUC zero-slot digest, gate absence, and baseline ACL, then fsyncs the old attempt's mode-`0600` final
state-v4 file as an immutable no-clobber archive; an existing archive must be byte-identical. The new call carries the
old pair digest as `previous_completion_digest`.

One owner transaction rechecks the exact old three-fact tuple plus archive digest, RESETs completion, atomically
replaces absent/old completed call with the new `gate_established` active call, establishes the gate, and revokes
CONNECT for PUBLIC/app/retention. It writes no pre-restore gate/failure AuditEvent; after commit it fsyncs only the
external state-v4 projection. A crash before that fsync is rebuilt from active gate + active call on another host.
An archive-before-CAS crash leaves the old completed idle untouched; a committed catalog transaction atomically
resets old completion, replaces the call, and establishes gate/ACL state, so no gate-only or missing-call window is
observable.
`needs_attention` cannot be overwritten. After commit it terminates existing
non-owner sessions and proves new app/retention denial. Both settings survive process/container crash. Different
manifests/kinds serialize through lifecycle/target locks and catalog gate; filesystem, Compose, environment and
process mutexes are never authority. Loss of control session makes the restore result unknown but does not prove the
psql backend exited; later phases wait for a matching invocation to reacquire the locks and reconcile catalog plus
the exact PID/backend-start/database/role/`PGAPPNAME` tuple recorded in authority. In
`restore_backend_starting`, only the original still-live local controller that proves ownership of the exact child/
pipe identity and SQL-byte-zero may continue to backend registration. Any other controller/host or missing/corrupt/
unverifiable local projection must CAS `needs_attention` (or persist only that disposition when CAS is unsafe), keep
`pg_restore_calls == 0`, and must not infer not-applied or allocate another ordinal. Ready/started/unknown phases must
prove the registered backend exited before fingerprint reads. `pg_stat_activity` alone is never authority.
Generic/sealed also hold the exclusive schema lifecycle lock after target admission for the
entire attempt/reconcile/reopen. Legacy remains isolated and does not enter this catalog transition graph.

The generic path passes `kind=generic` and the manifest SHA-256 to one image-internal maintenance executor. Its
credential-bearing child is fixed to target database, role `ai_employee_owner`, exact
`PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>`, and
`psql --set=ON_ERROR_STOP=1 --single-transaction`; its stdin is the controller-owned anonymous pipe. A second child
has all `PG*`/DSN/Secret values removed and runs exactly
`pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>` as a SQL generator.
The Python controller sends a transaction-local deferred completion-guard prelude, forwards generator stdout in
bounded chunks without a shell pipeline or SQL persistence/logging, and sends the completion trailer only after
generator EOF plus exit zero. Missing trailer, controller crash, either child failure, or pipe failure must make the
psql single transaction roll back. Ordinary 0019 backups remain valid without preflight/`pre-migration` artifacts.
Standalone role bootstrap always acquires/checks the common
lifecycle/catalog-authority boundary and refuses every active/`needs_attention` attempt. Restore-time object/schema grant SQL is exposed only as a function of the
already-held executor connection, never through a second owner process and never with an early CONNECT grant.

The operation-selected verifier also runs on that holder connection while gate is active and app/retention CONNECT
remains revoked: execute `SET ROLE ai_employee_app`, `BEGIN READ ONLY`, assert session/current user, require
SQLSTATE `25006` for DML, and finish every schema/revision/fingerprint/health/sampled-fact check before reopening.
No data-dependent or otherwise failing verifier may run after admission reopens. After all checks, CAS expected
`verified` to `reopen_committing` with a new independent `reopen_ordinal`. Freeze event type
`database.restore.completed`, metadata schema `ai_employee.database_restore_completed.v1`, and the exact key set:
`schema`, `attempt_id`, `kind`, `target_identity_digest_v1`, `source_binding_digest_v1`, `manifest_sha256`,
`previous_completion_authority_digest_v1` (JSON null from pristine), `final_call_ordinal`, `final_reopen_ordinal`,
`gate_established_at`, `final_call_started_at` (JSON null for the direct exact-post edge), `final_backend_start`
(JSON null when psql was never started), `expected_post_revision`, `expected_post_restore_fingerprint_v1`,
`observed_post_revision`, `observed_post_restore_fingerprint_v1`, `maintenance_gate_version`,
`maintenance_gate_value`, `completed_at`, and `completion_authority_digest_v1`, with no extra key. Gate version is
integer `1`; all timeline fields are safe database UTC six-microsecond values copied from the catalog call, and
`completed_at` is frozen in the final transaction. No gate-established/call-start/failure/needs-attention AuditEvent
is written before restore; catalog call + state-v4 are the crash evidence, and a later manual incident audit is
non-authoritative.

The completion digest covers the complete completed-call schema. Construct the exact `restore-call:v4` completed
value with its final digest slot set to exactly 64 ASCII zeroes; call those bytes
`canonical_completed_call_bytes`. Compute
`SHA-256(b"ai_employee.restore_completion_authority.v1\0" || canonical_completed_call_bytes)`, then replace only the
zero slot with that lowercase digest. Verification zeroes the stored slot again and requires the recomputed digest,
stored call digest, and completion-GUC digest to match. This fixed zero-slot rule avoids self-reference while binding
every completed-call fact.
The final owner transaction first locks and reads the complete `users` table and requires exactly one administrator
row; that row may be active or the inactive anonymized row retained by M1 all-data deletion, while zero or multiple
rows fail closed. It then CASes call authority to `completed` including the exact digest and inserts one ordinary
BigInteger-ID audit whose outer fields are exactly `user_id=<the sole users.id>`, `task_id=NULL`,
`event_type=database.restore.completed`, `actor_type=system`, `actor_id=database_restore`, and database-generated
`created_at`, with the exact versioned metadata and no extra key. The same transaction sets database-wide
`ai_employee.restore_completion=restore_completion:v1:<digest>`, resets the gate, and restores only minimal
app/retention CONNECT while PUBLIC stays revoked. AuditEvent ID is not part of canonical bytes, call authority, the
completion GUC, or ACK predicates. Extra/missing metadata, value/digest mismatch, a same-attempt/digest pre-existing
audit, or an INSERT count other than one rolls back; this is commit-time atomic audit evidence, not admission/ACK
authority. Ordinary retention and all-data deletion may later remove the audit without invalidating completion.
If commit ACK is unknown, close the old session, reacquire lifecycle/target locks in a new owner session, and read
both call and completion. A required valid completed call + expected GUC whose zero-slot digest is mutually bound,
gate absent, and baseline ACL proves committed and forbids replay; only audit/local projection may be absent. Because
every active attempt reset completion while establishing its gate, exact active `reopen_committing` + absent
completion GUC + matching gate + fully revoked ACL proves not applied and CASes by exact old call value to
`reopen_not_applied`. Only explicit operator approval may use a new `reopen_ordinal` to re-enter
`reopen_committing`; missing completed call, digest mismatch, completion with active gate, completed call with absent
completion, or any other tuple outside the three-state matrix is `needs_attention`. `completion absent + baseline ACL`
is legal only when call and gate are also absent (`pristine_idle`). A prior valid completion value is never legal
during an active attempt. General services remain stopped until an operator observes completed
and explicitly restarts them.

Freeze an independent absolute `RESTORE_STATE_DIR`; generic/sealed restore mounts backup and sealed artifacts
read-only, mounts only this directory read-write, and rejects any state path equal to or nested under those trees.
Projection path is `${RESTORE_STATE_DIR}/<target_identity_digest_v1>/<attempt_uuid>.json`, mode `0600`, parents
`0700`, schema `ai_employee.postgres_restore_state.v4`. It mirrors all three complete content-free catalog facts and
stable result code through same-directory temp, mode check, file/directory `fsync`, and atomic rename, but is never a
lock or transition/call/reopen authority. It is not uploaded and is outside backup retention/orphan cleanup. Another
host with no file, or a corrupt/stale file, reconstructs it from gate/call/completion facts. For completed state it
must read the non-optional completed call plus matching completion GUC, gate absence, and baseline ACL regardless of
whether the completion audit still exists; the completed projection becomes the immutable archive consumed by the
next admission, but never replaces the catalog call. Insufficient/mixed catalog facts return a
`needs_attention` disposition, persisting that phase only
when the frozen graph has the corresponding edge. Legacy does not use this projection.

Freeze the complete phase graph exactly:

```text
new → gate_established
gate_established → restore_backend_starting | restore_succeeded  # 后者只允许 exact post evidence，零 psql/pg_restore
restore_backend_starting → restore_backend_ready | needs_attention
restore_backend_ready → restore_started | restore_not_applied | needs_attention
restore_started → restore_succeeded | restore_outcome_unknown | restore_not_applied | needs_attention
restore_outcome_unknown → restore_succeeded | restore_not_applied | needs_attention
restore_not_applied → restore_backend_starting # 显式新 call_ordinal，且先证明 exact pre-state
restore_succeeded → grants_succeeded
grants_succeeded → verified
verified → reopen_committing
reopen_committing → completed | reopen_not_applied | needs_attention
reopen_not_applied → reopen_committing         # 新 reopen_ordinal
```

`completed`/`needs_attention` are terminal; reject jumps, regressions, and illegal self-transitions. Every database
transition compares exact old authority, attempt UUID, expected phase and `call_ordinal`; reopen transitions also
compare `reopen_ordinal`. Both ordinals are canonical `0..999999`; overflow is a fail-closed operator error before a
call and does not change the current phase, wrap or reuse an ordinal. The six-digit maximum keeps the exact PG
application name within 63 bytes. A grant rollback remains `restore_succeeded`; verifier failure remains
`grants_succeeded`; each has bounded retry without a fake phase update.

For a real call, first prove current revision/fingerprint exactly equal the frozen pre-state. In the same owner
transaction, CAS exact old authority from `gate_established|restore_not_applied` to `restore_backend_starting`,
increment the never-reused ordinal by exactly one, retain `reopen_ordinal`, write this ordinal's fresh database
`call_started_at`, reset backend PID/start to `-`, re-freeze pre revision/fingerprint from that exact current read,
and reset observed/completed fields to the starting shape. A retry from `restore_not_applied` must not copy the prior
ordinal's backend facts; this is the sole controlled ordinal-boundary clearing exception. Within the new ordinal,
ready/started/outcome retain its call-start, pre and backend facts. The direct exact-post ordinal-0 shape is unchanged.
The Python controller reads the long-lived bootstrap owner Secret only for the fixed-target
credential-bearing psql consumer. Spawn psql with exact database, role `ai_employee_owner`,
`PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>`, `--set=ON_ERROR_STOP=1`, and
`--single-transaction`; the controller retains the sole pipe write end, so psql receives no SQL while connecting.
From the lock-holder control session, require exactly one matching database/role/application backend, verify its PID
and six-microsecond UTC `backend_start` are newer than the spawn fence, then CAS
`restore_backend_starting → restore_backend_ready` and persist those facts. Only while gate, authority, backend
identity, and the controller-owned pipe still match may it CAS `restore_backend_ready → restore_started` and fsync
the v4 projection; SQL bytes and pg_restore calls must remain zero before that point.

After started, send the transaction-local deferred completion-guard prelude, spawn the credential-free pg_restore
generator, stream its stdout to psql, and send the guard trailer only after clean generator completion. A crash
before backend visibility closes the only pipe writer, so a late psql connection sees EOF and executes zero SQL;
ready-before-feed crash also executes zero SQL, while mid-stream crash or either child failure lacks the trailer and
rolls back. Before revision/fingerprint reconcile, `restore_backend_ready`/`restore_started`/unknown must prove the
authority-bound PID+backend_start+database+role+application backend exited. A starting phase without registered
backend may continue only under the original still-live local controller that proves the exact child/pipe identity and
SQL-byte-zero, and then only to `restore_backend_ready`. Any second host/controller without that proof must CAS
`needs_attention` (or persist only that disposition when CAS is unsafe), keep `pg_restore_calls == 0`, and may neither
infer exact pre/not-applied nor allocate a new ordinal. Exact post → `restore_succeeded`; exact pre from the registered
backend phases → `restore_not_applied`; partial/inconsistent/indistinguishable → `needs_attention`. Starting-phase
manual handling requires operator verification/termination of any child/backend and an explicit forward-fix, not an
automatic recovery claim. Only explicit operator approval plus a fresh exact-pre proof from legal
`restore_not_applied` may allocate a new `call_ordinal`; no blind replay. Test spawn-before-visible crash,
backend-ready-before-feed, SQL-byte-zero before started CAS+fsync, mid-stream crash/missing trailer rollback,
generator/consumer failure, commit ACK unknown, two-host overlap, stale PID/backend_start, cross-host no projection
and no local child/pipe proof (`pg_restore_calls == 0`, `needs_attention`), the sole original-controller
starting→ready path, partial/inconsistent outcomes, CAS races, and ordinal reuse rejection. Also test a second restore
of an older backup verifies/fsyncs the prior completed pair archive, then atomically replaces the old call and resets
the prior completion GUC while establishing the new active gate/call; a host crash immediately after the final commit
reconciles as completed without reopening or replay.

Pre-manifest backups must be rejected by production generic restore. Define the exact recipe
`just restore-legacy-to-isolated file output_basename`, backed only by
`scripts/convert-legacy-backup.sh` and dedicated Compose isolated PostgreSQL/conversion services. It is allowed only
for `APP_ENV=development|test`; `file` must be an exact regular non-symlink legacy dump with its verified adjacent
checksum, and `output_basename` must pass the ordinary safe-basename validator with no final-group conflict in the
controlled `${BACKUP_DIR}`.

Before creating any Docker or temporary resource, each invocation fsyncs and atomically publishes a mode-`0600`
registry record under `${BACKUP_DIR}/.legacy-conversion-registry/` (directory `0700`). It binds the canonical attempt
UUID, UTC creation time, `kind=legacy_conversion`, checksum-verified source binding, deterministic project/container/
network/volume names, controlled temporary Secret path, and exact labels. It then creates project name
`aiemployee-legacy-<32-lowercase-hex-uuid>` and runs only profile `legacy-conversion` services
`legacy-conversion-postgres` and `legacy-backup-converter`, with a project-private `internal: true` network,
ephemeral PostgreSQL volume, and generated mode-`0600` temporary database Secret at the registered path. Every
container/network/volume carries the same attempt/kind/created-at labels. Do not print the Secret or render it into
Compose/logs.
The isolated services must not attach to the workspace/default/production network, volume, hostname, or database.
Restore the legacy dump only into that PostgreSQL, run read-only actual Alembic revision and health verification,
then invoke the normal backup path against the isolated database to create a new manifest-bound group at the exact
output basename. Unknown/unsupported revision, unsafe name, output conflict, checksum failure, restore failure, or
health failure must produce a stable content-free failure and no new published group. Every conversion first runs
`just legacy-backup-scavenge`. A trap on success and every catchable failure/signal runs
`docker compose -p <project> --profile legacy-conversion down --volumes --remove-orphans`, deletes the registered
Secret/record, and verifies no labeled project resource remains. `scripts/scavenge-legacy-backups.sh` covers
SIGKILL, daemon crash, and host crash: after a fixed 3600-second grace it takes a per-attempt nonblocking lock,
cross-checks the registry against exact Docker labels, requires no active labeled container, and only then removes
that attempt's container/network/volume, Secret, and registry. Docker unavailability, mismatched labels, an active
container/lock, or a recent record must preserve everything; another active run is never touched. It must never
attach a fabricated manifest to old bytes, repair/stamp a revision, or connect/modify the workspace/production
database. The dedicated shell test plus PostgreSQL integration test must assert cleanup and zero sensitive output.
Generic, sealed, and legacy recipe/service/script/fixture/test paths must not invoke one another or reuse incompatible
artifacts/preconditions.

For `just calendar-aad-restore-0018 file`, require the stopped-service sealed window, preflight plus exact
`pre-migration` artifact, exact local image content ID, owner-before-secret zero-call mismatch behavior,
`--pull never`, image-internal executable digests, the independent sealed validator/service, revision 0018, and
deterministic artifact comparison. After its extra guard, sealed must pass `kind=sealed_0018` into the exact same
management/target/exclusive-schema locks, database gate/call/completion facts, state-v4 projection, Python-managed
psql backend registration plus pg_restore SQL-stream/rollback reconciliation, same-session verifier, and
completion-GUC atomic-reopen executor as generic.
The sealed verifier callback performs `SET ROLE ai_employee_app` + `BEGIN READ ONLY`/SQLSTATE `25006` before reopen;
there is no standalone post-reopen app-only verifier. Assert generic/sealed/legacy recipes, validators, artifacts,
fixtures, and outputs remain non-interchangeable. All import the same admission helper, but only generic/sealed import
the restore state/reconcile/reopen executor.

Before implementing audit tooling, add a release-wrapper contract that fails because
`scripts/test-m2-release.sh` does not yet explicitly execute:

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test bash scripts/test-calendar-aad-0019-audit.sh
~~~

The contract must inspect/execute that direct call; `just ci` is not accepted as indirect evidence. Task 30 later
moves the fixed database value to a script-wide validated export while preserving this direct audit subprocess.

- [ ] **Step 2: Run the backup/restore/audit RED gate**

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/operations/test_database_maintenance_gate.py \
  backend/tests/integration/operations/test_postgres_backup_restore.py \
  backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py \
  backend/tests/integration/retention/test_role_permissions.py \
  backend/tests/unit/test_init_db_roles_script.py -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
bash scripts/test-calendar-aad-0019-audit.sh
bash scripts/test-legacy-backup-conversion.sh
bash scripts/test-tooling.sh
bash scripts/test-deployment.sh
git diff --check
~~~

Expected: FAIL because backup publication has no database/global-directory serialization, unique run staging,
schema-revision shared lock/CAS, versioned manifest/fingerprint/script-digest semantics, or race-safe orphan handling;
the immutable image does not contain every operations entrypoint; lifecycle wrappers/reset do not share the
management lock or catalog gate/call/completion facts, and direct drop/create can bypass a crashed restore. Generic restore
has no `.env.example`/read-only-artifact/separate-state contract, database-authoritative CAS phase graph, one-time
psql backend registration plus SQL-byte-zero/start fence, deferred completion-guard stream rollback, DB-zero-write
kind-aware no-clobber already-applied evidence, active-attempt completion RESET, completed-call/completion-GUC/gate/ACL proof,
required completed-call pairing, closed admission matrix, prior-pair archive, and zero pre-restore gate audits,
cross-host projection rebuild and starting fail-closed, ordinal-safe retry with a fresh call-start/pre freeze and
controlled old-backend reset, `reopen_not_applied`, same-session
verifier, or atomic reopen ACK reconciliation; legacy conversion lacks registry/labels/scavenging; sealed does not
reuse the shared primitive; audit tooling and the explicit release-wrapper call are absent.

- [ ] **Step 3: Implement the generic manifest/restore boundary, sealed path, and audit tooling**

In `scripts/backup-postgres.sh`, first acquire Task 16A's target lifecycle lock from management database `postgres`,
then `BACKUP_LIFECYCLE_LOCK=(20260806, 274)` and the fixed `${BACKUP_DIR}` `flock` in order, create a unique
`.partial.<uuid>` staging area, and retain all locks through local/remote
publication, retention, and cleanup. Acquire the Task 16A schema-lifecycle shared lock before reading pre-revision,
hold it through `pg_dump`, post-revision equality CAS, and local final-manifest publication, then release it without
changing Task 16A's typed guard. Create all group members under the unique staging names, finalize the manifest
before writing the checksum that covers dump plus manifest, verify the complete binding, set `0600`, no-clobber
publish dump/checksum, and rename the manifest last. Treat an existing final member as an all-group conflict.
Use unique remote staging, remote manifest last, fixed 3600-second local/remote orphan grace, and lock-held mtime/
manifest rechecks immediately before deletion. Resolve immutable backend image content ID in the host recipe and
pass it as data, never as operator authority. Compute and bind the expected post `restore_fingerprint_v1` plus the
allowlisted image-internal executable digest map. A partial local/remote group or pre/post revision mismatch is not
a successful backup; cleanup removes only current-run or revalidated expired orphan state. Modify
`backend/Dockerfile` so the built immutable image actually COPYs every Task 27D runtime script/CLI from repository
root context; Compose must execute only those fixed paths and reject executable bind mounts.

Keep `scripts/restore-postgres.sh` free of 0019 preflight/`pre-migration` logic. Add one operations-profile generic
owner maintenance/restore service that does not inherit `backend-common` or auto-run migration. Extend Task 16A's
`database_maintenance.py` and existing `cli/database_maintenance.py` with strict matching gate/call-authority claims,
CAS transitions, exact psql backend identity registration, state-v4 projection, DB-zero-write/local already-applied
evidence, ordinal reopen, the frozen `restore-call:v4` parser/length rules, required completed-call/completion-GUC
pair verification, prior-pair archive, and post-restore-only completion audit timeline. Add
`infrastructure/db/postgres_restore_stream.py` for the controller-owned anonymous pipe, deferred completion guard,
credential-free pg_restore generator, bounded forwarding, child shutdown, and exact status classification. The
parser/transition validator must enforce same-ordinal fact preservation plus the sole legal new-ordinal starting
reset: fresh call-start, exact-current pre re-freeze, backend clearing, observed/completed starting-shape enforcement,
and no prior PID/start inheritance. Add
`cli/postgres_restore.py` as the typed image-internal entrypoint. Before owner startup, `just restore` validates manifest
group, effective switches, all consumers, exact target and second production confirmation. It mounts backup/artifact
read-only and a separately validated `RESTORE_STATE_DIR` read-write. The executor holds management lifecycle, target,
then exclusive schema lifecycle lock, owns gate/CONNECT revocation, database-authoritative v4 projection, exact
`PGAPPNAME` psql registration, `restore_backend_starting`/`restore_backend_ready`/`restore_started` CAS+fsync gates,
credential-free pg_restore-to-psql streaming, same-session grants/verifier, and exact completion-authority/GUC plus
fixed outer/schema/key/value/digest audit insertion and gate/ACL final transaction. ACK loss uses a new owner session
and the completed-call/completion-GUC/gate/ACL completed vs
`reopen_not_applied` matrix.
Update `.env.example` and Compose with the fixed development/test host default plus production-explicit
`RESTORE_STATE_DIR` mount/permission contract; never persist a database credential in state or expose it to
the pg_restore generator or the broad service environment. The executor
must pass the long-lived bootstrap owner credential only to the fixed-target psql consumer; the pg_restore generator
must have all `PG*`/DSN/Secret material removed and only emit SQL to stdout. It must prove zero SQL bytes before the
started CAS+projection fsync, roll back on missing completion trailer/controller crash/either-child failure, and prove
the authority-bound backend exited before fingerprint or a new ordinal. It must also implement the no-gate
`restore_already_applied` DB-zero-write local evidence, the `gate_established → restore_succeeded` evidenced edge,
and the exact ordinary-ID completion audit insertion plus completed-call/completion-GUC/gate/ACL authority contract
below. Gate establishment must persist only catalog gate/call plus state-v4, never a pre-restore AuditEvent.
Make ordinary
`scripts/init-db-roles.sh` acquire/check the same lifecycle/catalog-authority boundary and reject every active or
`needs_attention` restore; expose restore grant logic
only as a callable on the existing holder session. Do not retain `AI_EMPLOYEE_RESTORE_CONNECT_FENCE` as a second
authority.

Keep Task 16A's `just db-reset` lifecycle wrapper intact and extend its tests: no direct drop/create may return, and a
crashed restore with released advisory sessions but active/`needs_attention` catalog authority must produce zero
drop/create/migration. Backup/migration/restore/reset use the fixed management→target→schema order; nested reset
migration consumes the live typed lease instead of reacquiring it.

Implement `scripts/convert-legacy-backup.sh`, the isolated Compose services, and
`just restore-legacy-to-isolated file output_basename` exactly as the non-production disposable-target conversion
above. Publish the durable registry before resources, apply exact labels, run the scavenger before each conversion,
and let `scripts/scavenge-legacy-backups.sh` recover SIGKILL/daemon/host-crash leftovers only after grace, per-attempt
lock, registry/label cross-check and active-container recheck. The trap handles normal/catchable cleanup. It produces
a new backup through the normal locked path; it is not an obsolete-0019 repair recipe and cannot manufacture a
manifest.

Add the separate sealed script/service/recipe and validator, preserving its full owner-before-secret,
exact-image/script guard, but route `kind=sealed_0018` through the same maintenance executor and same-session sealed
verifier callback. Keep generic/sealed/legacy artifacts and validators separate. Implement all three audit phases and make the initial
`scripts/test-m2-release.sh` call the audit test explicitly with `TEST_DATABASE_URL`; Task 30 may add tests around it
and move the value to a validated script-wide export, but may not remove or hide the direct audit execution. Update
`docs/operations.md` as a target contract with explicit implementation-status warnings.

- [ ] **Step 4: Verify and commit Task 27D**

Re-run Step 2, inspect the full backup/restore/operations diff, then:

~~~bash
git diff --check
git add .env.example backend/Dockerfile compose.yaml \
  backend/src/ai_employee/infrastructure/db/database_maintenance.py \
  backend/src/ai_employee/infrastructure/db/postgres_restore_stream.py \
  backend/src/ai_employee/cli/database_maintenance.py \
  backend/src/ai_employee/cli/postgres_restore.py \
  backend/src/ai_employee/cli/verify_restored_backup.py \
  backend/src/ai_employee/cli/calendar_aad_verify_restored_0018.py \
  scripts/backup-postgres.sh scripts/restore-postgres.sh scripts/init-db-roles.sh \
  scripts/convert-legacy-backup.sh scripts/scavenge-legacy-backups.sh \
  scripts/test-legacy-backup-conversion.sh scripts/restore-calendar-aad-0018.sh \
  scripts/audit-calendar-aad-0019.sh scripts/test-calendar-aad-0019-audit.sh \
  justfiles/ops.just justfiles/db.just scripts/test-m2-release.sh scripts/test-tooling.sh scripts/test-deployment.sh \
  backend/tests/unit/test_init_db_roles_script.py \
  backend/tests/integration/retention/test_role_permissions.py \
  backend/tests/unit/infrastructure/db/test_postgres_restore_stream.py \
  backend/tests/integration/operations/test_database_maintenance_gate.py \
  backend/tests/integration/operations/test_postgres_backup_restore.py \
  backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py docs/operations.md
git commit -m "fix: bind and separate database restores"
~~~

### Task 27E: Integrate retention, privacy deletion, operations, and acceptance

**Depends on:** Tasks 27A–27D. This is the integration/operations closeout; it does not add provider behavior,
migration branches, or restore services.

**Files:**
- Modify: `backend/src/ai_employee/workers/retention.py`
- Modify: `backend/src/ai_employee/workers/privacy.py`
- Modify: `backend/src/ai_employee/application/use_cases/privacy.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/diagnostics.py`
- Modify: `backend/tests/integration/privacy/test_source_cache_cleanup.py`
- Modify: `backend/tests/integration/privacy/test_all_data_deletion.py`
- Create: `backend/tests/integration/retention/test_m2_action_retention.py`
- Modify: `docs/operations.md` — extend, without replacing, Task 27D's manifest/restore sections with the final
  retention/privacy/operator integration.
- Modify: `docs/acceptance-checklist.md`

- [ ] **Step 1: Write failing retention, deletion-barrier, and operations-consistency tests**

Cover every M2 encrypted content group, CalendarEvent four-column atomic clear, source-cache cleanup, all-data write
barrier/final reconcile, user isolation, and the shared versioned result-union retention rules. Unresolved automatic
fences survive cutoff; automatic-success, unsatisfied-recovery, and successful-recovery groups delete only when every
member is older than cutoff; parser mismatches remain protected; cleanup never depends on later credential lineage.
`database.restore.completed` follows ordinary 365-day audit retention and all-data deletion; it has no
catalog-authority exemption. Tests must delete an old completion audit while preserving the valid completed call +
`ai_employee.restore_completion=restore_completion:v1:<digest>` pair, then prove completed admission/ACK still uses
that mutually bound pair, gate absence, and baseline ACL. The catalog call is not user-scoped AuditEvent data and
must not be deleted by retention/privacy. All-data deletion must remove the user's completion audit while retaining the single
inactive anonymized `users` row required by M1, and later restore completion must still be able to bind a new audit to
that sole row. AuditEvent's ordinary BigInteger ID, row count, and metadata are not admission/ACK authority.
Add documentation checks for the current-expiry effective deadline, four-entry access-log canary, frozen
Task-16A/Task-27C migration boundary, Task 27D schema/backup locks plus manifest-bound publication, production
management/target lifecycle locks, three catalog facts, `.env.example`/state-v4, DB-zero-write local already-applied
evidence, psql backend registration plus deferred-guard pg_restore stream rollback/ordinal generic restore,
active-attempt completion RESET, completed-call retention/zero-slot verification, and pair/gate/ACL-authoritative atomic reopen,
registry-scavenged isolated
legacy conversion, and artifact-distinct sealed restore on the same generic/sealed restore primitive,
obsolete-0019 non-production
reset/production-stop disposition, and explicit release audit gate. This
closeout may integrate those operator-facing facts but must not re-own their tooling implementation.

- [ ] **Step 2: Run the retention/privacy RED gate**

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/retention/test_m2_action_retention.py \
  backend/tests/integration/privacy/test_source_cache_cleanup.py \
  backend/tests/integration/privacy/test_all_data_deletion.py -q
~~~

Expected: FAIL because M2 content groups, fence-aware cleanup, ordinary completion-audit retention/privacy deletion,
deletion barrier, and the finalized operations/acceptance contract are incomplete.

- [ ] **Step 3: Implement lifecycle cleanup and freeze operator-facing contracts**

Implement the detailed retention/privacy rules below, update `docs/operations.md` and
`docs/acceptance-checklist.md` as target contracts rather than claims that production 0019 already ran, and retain
all security invariants delivered by 27A–27D. Do not add a retention-only completion parser or protect completion
audits through catalog facts; restore completion authority remains in the database-wide completed call + completion
GUC pair while AuditEvent follows ordinary user-scoped cleanup/privacy behavior. Do not move implementation back into
this closeout task.

- [ ] **Step 4: Verify and commit Task 27E**

~~~bash
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/retention/test_m2_action_retention.py \
  backend/tests/integration/privacy/test_source_cache_cleanup.py \
  backend/tests/integration/privacy/test_all_data_deletion.py -q
git diff --check
git add backend/src/ai_employee/workers/retention.py backend/src/ai_employee/workers/privacy.py backend/src/ai_employee/application/use_cases/privacy.py backend/src/ai_employee/infrastructure/db/repositories/diagnostics.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py backend/tests/integration/retention/test_m2_action_retention.py docs/operations.md docs/acceptance-checklist.md
git commit -m "feat: protect m2 lifecycle data"
~~~

#### Detailed protocol reference for Tasks 27A–27E

The following inventory and detailed test/implementation clauses are normative cross-task constraints. They are not
a staging list or a sixth combined task; the task-local `Files`, RED/GREEN gates, and commits above are
authoritative.

**Cross-task file inventory (reference only):**
- Modify: `backend/src/ai_employee/workers/retention.py` — atomically clear every CalendarEvent description/location four-column field group as well as existing M2 content groups, replace generic AuditEvent deletion with user-scoped fence-aware cleanup for unresolved 0019 refresh attempts, and keep `database.restore.completed` on the ordinary audit cutoff without a completion-GUC exemption.
- Modify: `backend/src/ai_employee/workers/privacy.py`
- Modify: `backend/src/ai_employee/application/use_cases/privacy.py`
- Create: `backend/src/ai_employee/application/ports/oauth_refresh.py` — provider-neutral automatic-refresh claim plus explicit-recovery connection lease, committed two-channel state machine, `calendar_aad_preflight | provider_refresh` automatic source union, zero-call unknown/retry contract, and new-session read-only `OAuthRefreshResultV1` reconciliation after commit ACK loss. The versioned result union maps automatic started to confirmed/replacement and recovery started to unsatisfied/replacement, validates each result's identity-change relationship before closure, and keeps append-only closure independent from current readiness.
- Create: `backend/src/ai_employee/application/ports/credential_rotation.py` — typed connection-generation plus access/refresh physical snapshots and fixed identity version, versioned confirmed/unsatisfied/replacement result contracts with `refresh_identity_changed: bool`, and a separate current-readiness result including stable `oauth_credential_state_conflict`; confirmed enforces disposition/flag/identity-equality consistency, replacement enforces old != new plus changed=true, and unsatisfied has no credential post/expiry fields.
- Modify: `backend/src/ai_employee/application/use_cases/connections.py` — extend connection-bound progressive authorization with the F/S/T unresolved-fence protocol; create `OAuthAttempt` plus `oauth.refresh_recovery_authorization_started` in the start transaction, keep the one-time code exchange outside the transaction, compare old/new refresh plaintext in controlled memory, reuse/extend `mark_progressive_authorization_failed` so safely known denial/missing/same/provider failures converge requested capabilities from `authorizing` to `action_required` at target `T` while stale `T` is a no-op, persist the matching unsatisfied result without closing the original fence, and atomically append replacement consumption only for a genuinely different non-empty token. A targetless callback that resolves to a fenced connection must fail before local credential/scope/capability persistence.
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_preflight.py` — 0018 affected-pair discovery, revision-global plus connection-scoped coordinator leases, append-only durable refresh fence with identity/key-version metadata, per-connection OAuth refresh through the shared snapshot-CAS/confirmed rotation port, local recoverability proof, and provider-resource-read-only probe orchestration.
- Create: `backend/src/ai_employee/application/calendar_aad_digests.py` — pure versioned field framing plus `credential_snapshot_digest_v1`, `refresh_credential_snapshot_digest_v1`, and `rollout_digest_v1` helpers shared by fence creation, replacement proof, replay lookup, retention, rollout guards, and evidence.
- Create: `backend/src/ai_employee/application/oauth_refresh_identity.py` — fixed `APP_MASTER_KEY_FILE` plus `AeadCipher.key_version` HKDF-SHA256/HMAC-SHA256 `refresh_token_identity_v1` derivation; M2 has no independent identity Secret, keyring, or root-key rotation, and plaintext/key material never leaves controlled memory.
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_rollout.py` — fixed 900-second deadline derivation, typed zero/no-deadline state, immutable-image binding, content-free rollout-state validation, and reusable start/pre-commit guards.
- Create: `backend/src/ai_employee/application/use_cases/calendar_aad_recovery.py` — marker-only recovery planning, pair-digest/recovery-ordinal state protocol, and exact-task execution orchestration.
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py` — add a marker-gated exact-event-scope entry that never invokes directory discovery.
- Modify: `backend/src/ai_employee/application/use_cases/sync_mail.py` — route ordinary mail refresh through the shared coordinator and preserve access-only/same-plaintext semantics without a second retry writer.
- Create: `backend/src/ai_employee/infrastructure/db/repositories/credential_rotation.py` — session-bound shared snapshot loader/CAS helper with connection → access row → refresh row → matching started audit locks, automatic `oauth.refresh_confirmed`, progressive replacement proof, target-`T` capability convergence plus versioned `oauth.refresh_recovery_unsatisfied`, strict event ordering, byte-preserving missing/same refresh handling, a single strict result parser for reconcile/retention, and independent current-readiness validation whose old-identity rollback guard applies only to changed=true results.
- Create: `backend/src/ai_employee/infrastructure/db/repositories/oauth_refresh_coordinator.py` — connection-scoped PostgreSQL session-advisory lease, automatic committed-started claim lookup, explicit recovery lease, lease-loss/unknown classification, exactly-once automatic provider-call admission used by preflight and mail/calendar writers, and new-session `user_id + connection_id + attempt_id` result-union reconciliation that never requires the lost lease or historical post-state equality.
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_preflight.py` — 0018 historical-triple scan, dedicated PostgreSQL session advisory lease, existing AuditEvent-backed automatic started/confirmed plus progressive replacement-consumption lookup and local recoverability projections without Calendar fact mutation; all credential commits delegate to the shared rotation repository.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/connections.py` — at progressive start validate the unique unresolved fence/current refresh snapshot before the existing `S→T=S+1` increment, create the target-`T` OAuthAttempt and recovery-started event in the same transaction, resolve callback recovery linkage by `OAuthAttempt.id`, and compose the shared session-bound rotation helper so credentials/scopes/capabilities and `source="progressive_recovery"` replacement proof commit atomically while generation stays `T`; extend `mark_progressive_authorization_failed` so matching-`T` unsatisfied results atomically set only requested capabilities to `action_required` with a stable error while preserving actual scopes/last-verified facts, and stale `T` is a capability no-op plus safe audit.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/email.py` — replace the unconditional `rotate_access_token` upsert boundary with snapshot loading/delegation to the shared credential-rotation repository; preserve existing mail-sync repository responsibilities without retaining a second credential writer.
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_aad_recovery.py` — user-owned marker scan, exact cursor locking, ordinal allocation, and atomic TaskRun/AuditEvent/Outbox creation.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py` — require an existing exact marker cursor and clear it only through final marker-aware CAS.
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/diagnostics.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/src/ai_employee/api/deps.py` — construct `AeadCipher` and `refresh_token_identity_v1` service from the same `APP_MASTER_KEY_FILE` bytes and fixed key version used by every Worker/CLI composition root.
- Modify: `backend/src/ai_employee/api/routers/connections.py` — make Google callback accept exactly one of `code+state` or any bounded non-empty `error+state`; reject missing state, code/error ambiguity, neither value and malformed error before state consumption. Every valid error, including an unknown name, consumes state once and routes target-bound recovery through the unsatisfied boundary. Preserve the safe classification matrix instead of collapsing Problems: Microsoft consent evidence remains `microsoft_admin_consent_required` with existing administrator guidance, user denial maps to `oauth_authorization_failed`, ordinary Microsoft `interaction_required` remains `microsoft_reauthorization_required`, and only unknown names fall back to `oauth_authorization_failed`. Raw error/description/codes never become persistent error codes or logs, replay is rejected, and targetless fenced-identity blocking stays before persistence.
- Modify: `backend/src/ai_employee/workers/sync_mail.py` — carry the read generation and complete access/refresh snapshots through Google/Microsoft automatic refresh and commit every known-valid missing/same/different response through the shared confirmed path; missing/same preserves the refresh row byte-for-byte and different never consumes an older fence.
- Modify: `backend/src/ai_employee/workers/sync_calendar.py` — use the same fixed-key identity service, coordinator and automatic confirmed rotation path for Google/Microsoft refresh, then validate/execute the dedicated ordinal-aware `calendar.aad_0019.resync` task kind without the legacy directory default.
- Modify: `backend/src/ai_employee/workers/execute_task.py` — route the dedicated recovery kind to the marker-gated Calendar step.
- Create: `backend/src/ai_employee/cli/calendar_aad_preflight_0019.py` — no-argument, 0018-compatible active-refresh plus provider-resource-read-only preflight with content-free rollout state, constructing AEAD and identity services from the same fixed `APP_MASTER_KEY_FILE` bytes/key version as API and Workers.
- Create: `backend/src/ai_employee/cli/calendar_aad_migrate_0019.py` — no-argument, rollout-state/typed-guard-bound exact 0018-to-0019 migration wrapper.
- Create: `backend/src/ai_employee/cli/calendar_aad_0019.py` — content-free one-off recovery CLI using the existing durable runner and an exact-task in-process dispatch boundary.
- Create: `backend/src/ai_employee/cli/calendar_aad_verify_restored_0018.py` — sealed-specific revision/artifact
  verifier callback invoked on the maintenance executor's already-held owner connection after
  `SET ROLE ai_employee_app` and `BEGIN READ ONLY`; it is not a standalone post-reopen app-only service.
- Read-only dependency: `backend/migrations/versions/20260809_0019_calendar_event_field_aad_v2.py` — Task 16A
  already freezes mutation-time guard invocation; Tasks 27A–27E must not edit it.
- Read-only dependency: `backend/migrations/env.py` — Task 16A already freezes the exact
  `on_version_apply` final guard; Task 27C only injects the concrete guard.
- Modify: `backend/tests/integration/privacy/test_source_cache_cleanup.py`
- Modify: `backend/tests/integration/privacy/test_all_data_deletion.py`
- Create: `backend/tests/integration/retention/test_m2_action_retention.py` — M2 retention, CalendarEvent four-column atomic-clear regressions, and cross-cutoff/user-isolated 0019 refresh-fence cleanup races.
- Create: `backend/tests/unit/application/test_calendar_aad_digests.py` — independent fixed canonical-byte/base64/SHA-256 vectors for all three v1 digest protocols; expected constants must not call production helpers.
- Create: `backend/tests/unit/application/test_oauth_refresh_identity.py` — independent HKDF/HMAC implementation of the fixed synthetic vector, fixed APP root-key/version construction parity, same-plaintext re-encryption preserving identity/changed=false, changed=true A→B→A rollback, and missing/changed-key fail-closed cases.
- Create: `backend/tests/unit/application/test_oauth_refresh_coordinator.py` — two-channel state-machine RED/GREEN tests for automatic claim ordering/committed-started admission, explicit recovery lease without a second started, strict confirmed/unsatisfied/replacement result-union parsing including disposition/flag/equality mismatch rejection, lock loss, unknown/CAS/definite-rollback handling, commit-ACK-loss read-only reconciliation, closure/readiness separation, and Taskiq/`TransientProviderError` zero-call delivery.
- Create: `backend/tests/integration/m2/test_oauth_refresh_coordinator.py` — PostgreSQL session-lease, dual-Worker single-call/confirmed arbitration, response-after-lock-loss, CAS-miss, cross-basename fence, explicit-recovery lease, confirmed/unsatisfied/replacement actual-commit-versus-actual-rollback reconciliation, ACK-lost-then-later-valid-refresh/reauth races, changed=false access-only/same-plaintext-re-encryption non-conflict, and changed=true A→B→A rollback-conflict zero-call tests.
- Modify: `backend/tests/unit/application/test_connection_capability_use_cases.py` — controlled-memory `secrets.compare_digest`, `F=S`/`S>F` start/target derivation, repeated explicit recovery attempts, no-token/empty/same-plaintext/access-only/disconnected negatives, denial/missing/same capability convergence, stale-`T` no-op, new-attempt retry, and different-token replacement orchestration.
- Modify: `backend/tests/integration/m2/test_connection_capability_repository.py` — unique-fence/current-refresh start CAS, OAuthAttempt plus recovery-started atomic creation, target-generation anti-replay, versioned unsatisfied result with no credential post/expiry, current-`T` `action_required` preserving actual scopes/last-verified facts, stale-`T` no-op, later different-token replacement atomic commit, result-union ACK-loss actual commit/rollback, later-reauth race closure, and injected definite rollback points without a callback generation increment.
- Create: `backend/tests/integration/m2/test_credential_rotation_repository.py` — complete-row/generation CAS, fixed lock order, automatic confirmed missing/same/different handling with strict disposition/flag/equality parsing, access-only byte preservation, progressive old != new/changed=true replacement proof, strict timestamps, result-union ACK-loss reconciliation, closure independent of later valid credential changes, changed=false access-only/same-plaintext-re-encryption non-conflict, `oauth_credential_state_conflict` for changed=true old-identity rollback/missing-row/invalid-state cases, and callback/Worker arbitration without Worker consumption of an old fence.
- Modify: `backend/tests/integration/api/test_connections.py` — Google and Microsoft callback query-shape regressions for required state, mutually exclusive code/error, pre-consumption shape rejection, all-valid-error state consumption, safe Problem/error-code classification for denial, Microsoft consent evidence, ordinary `interaction_required`, and unknown names, raw-error redaction, and same-state unknown-error replay rejection.
- Modify: `backend/tests/integration/google/test_oauth_flow.py` — Google F/S/T multiple explicit recovery attempts, any valid error callback state consumption, denial/unknown fallback to `oauth_authorization_failed`, denial/missing/same capability convergence, stale-`T` no-op, first unsatisfied/second different-token success, unsatisfied/replacement ACK-loss actual commit/rollback, later-reauth closure race, targetless fenced-identity pre-save blocking, stale/concurrent target rejection, and transaction-result reconciliation.
- Modify: `backend/tests/integration/microsoft/test_oauth_flow.py` — Microsoft parity for valid-error state consumption/redaction/replay rejection while preserving `microsoft_admin_consent_required`, `microsoft_reauthorization_required`, and generic `oauth_authorization_failed` classifications; repeated explicit recovery, denial/missing/same capability convergence, stale-`T` no-op, targetless pre-save blocking, stale/concurrent target rejection, and result-union reconciliation.
- Modify: `backend/tests/integration/google/test_gmail_sync.py` — Google automatic `provider_refresh` claim, access-only confirmed with byte-preserved refresh row, different-token confirmed while any older unresolved fence remains closed to automatic entry, stale CAS, lock loss, unknown/Taskiq zero-call delivery, and no replay of the original unknown preflight attempt.
- Modify: `backend/tests/integration/google/test_calendar_sync.py` — Google calendar automatic coordinator parity for shared snapshot-CAS confirmed rotation and zero-call unresolved-fence blocking.
- Modify: `backend/tests/integration/microsoft/test_mail_sync.py` — Microsoft automatic refresh missing/same/different confirmed behavior through the shared coordinator, lock/unknown handling, and no old-fence consumption.
- Modify: `backend/tests/integration/microsoft/test_calendar_sync.py` — Microsoft calendar automatic coordinator parity, multi-refresh single-call behavior, same-plaintext/access-only preservation, different-token confirmed, and unresolved-fence zero-call blocking.
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_preflight.py` — 0018 local proof, attempt/dual-digest-bound generic automatic started/confirmed fence, strict identity-change result parsing, versioned result-union commit-ACK-loss actual-commit versus actual-rollback reconciliation, closure/readiness separation with later valid refresh/reauth, changed=false non-conflict and changed=true rollback-conflict zero-call cases, explicit progressive capability-converging replacement gate, repeated recovery, classified Google/Microsoft callback-error parity, targetless pre-save blocking, crash recovery, proactive OAuth refresh/scope/deadline boundary, Fake/HTTP-mocked real-adapter probes, and no-Calendar-mutation coverage.
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_recovery.py` — marker isolation, ordinal-aware atomic task creation, exact provider reads, explicit retry, concurrency, and CLI lifecycle coverage.
- Create: `backend/tests/integration/operations/test_postgres_backup_restore.py` — fixed global/local backup locks,
  unique staging, schema-revision shared-lock/CAS, local/remote cleanup races, management/target locks, database
  three-state gate/`restore-call:v4`/completion-GUC pair authority, low/high system-ID vectors, `.env.example`/v4 projection,
  DB-zero-write local already-applied evidence, psql starting/ready/started registration, SQL-byte-zero and deferred
  completion-guard stream rollback, cross-host starting fail-closed, authority-bound backend/ordinal reconcile,
  prior-completed-pair archive, no pre-restore gate audit, fixed-outer-field completion audit insertion plus
  completed-call/completion-GUC/gate/ACL-authoritative reopen, session-role
  verifier, and registry-scavenged legacy conversion.
- Create: `backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py` — current-expiry
  effective-deadline tightening, zero-state/image binding, and sealed owner-before-secret validation followed by the
  shared maintenance executor and same-session sealed verifier.
- Modify: `compose.yaml` — add generic/sealed owner maintenance boundaries plus isolated legacy PostgreSQL/
  conversion services; only sealed uses artifact-bound exact image, and no service bind mounts an executable.
- Modify: `.env.example` — development/test host default plus production-explicit absolute
  `RESTORE_STATE_DIR`/fixed container mount and safe owner/mode/no-symlink/non-overlap contract; no Secret values.
- Modify: `docs/operations.md` — generic backup three-file manifest publication, remote/retention grouping,
  production stopped-write restore and legacy isolation; revision-global lease, automatic started/confirmed,
  commit-ACK-loss versus definite-rollback reconciliation, repeated explicit progressive recovery with
  capability-converging started/unsatisfied/replacement facts, Google error-callback parity, targetless pre-save
  blocking, fixed-root identity ordering, shared snapshot CAS, whole-group cross-cutoff fence retention, typed
  rollout guard and image binding, non-rolling 0019 rollout, ordinal-aware recovery, sealed owner-before-secret
  exact-image restore, shared management/target/exclusive-schema locks, database gate/call/completion facts,
  state-v4 projection, DB-zero-write local already-applied evidence, psql backend registration plus credential-free
  pg_restore SQL stream/deferred-guard rollback and ordinal reconcile, completed-call/completion-GUC/gate/ACL-authoritative reopen
  with exact audit insertion,
  operation-specific
  same-session role-switched verifiers,
  rollback floor, and incident handling.
- Modify: `docs/acceptance-checklist.md` — three fixed digest vectors, automatic confirmed plus progressive replacement atomicity, commit-result actual-commit/rollback reconciliation, repeated capability-converging recovery, Google callback-error and targetless-block evidence, shared snapshot-CAS and whole-group cross-cutoff durable refresh-fence evidence, image/artifact guard with zero owner calls on mismatch, per-ordinal scope recovery, exact-image owner restore/grant/database-read-only app verification, and normal rollback-floor acceptance.
- Create: `scripts/audit-calendar-aad-0019.sh` — content-free, read-only three-phase 0019 local-proof and data-integrity audit.
- Create: `scripts/test-calendar-aad-0019-audit.sh` — Task 13 synthetic-database contract test for local recoverability failures, audit phases, and artifact redaction.
- Modify: `backend/Dockerfile` — COPY every generic/sealed/legacy/audit/maintenance runtime entrypoint into fixed
  immutable-image paths from repository-root context; executable bind mounts are forbidden.
- Modify: `backend/src/ai_employee/infrastructure/db/database_maintenance.py` — preserve exact digest/lifecycle
  vectors and add strict gate/call/completion catalog parsing/CAS, target-lock ownership, matching-attempt claims,
  exact PID/backend-start/database/role/`PGAPPNAME` discovery, DB-zero-write already-applied evidence,
  `restore-call:v4` length/encoding/parser, completed-call zero-slot proof and new-session pair/gate/ACL
  reconciliation, with audit validation scoped to
  the final transaction rather than admission/ACK.
- Create: `backend/src/ai_employee/infrastructure/db/postgres_restore_stream.py` — controlled psql consumer and
  credential-free pg_restore generator orchestration, controller-owned anonymous pipe, deferred completion guard,
  bounded forwarding, two-child shutdown/status collection, and zero-SQL/rollback failure classification.
- Modify: `backend/src/ai_employee/cli/database_maintenance.py` — extend Task 16A's lifecycle wrapper with shared
  holder-session gate/ACL setup, catalog-authoritative restore intent, starting/ready/started backend registration,
  authority-bound backend exit reconciliation, DB-zero-write already-applied handling, grant/verifier callbacks,
  and active-attempt completion RESET plus completed-call/completion-GUC/gate/ACL-authoritative atomic reopen.
- Create: `backend/src/ai_employee/cli/postgres_restore.py` — typed image-internal generic/sealed restore entrypoint
  that composes the maintenance authority and stream executor without a shell pipeline.
- Modify: `scripts/backup-postgres.sh` — hold fixed database backup lock plus `${BACKUP_DIR}` flock across unique-run
  staging/publication/remote/retention/cleanup, bind pre/post revision to `pg_dump` under the Task 16A shared schema
  lock, and publish each ordinary or explicit-0019 backup as a `0600` dump/checksum/versioned-manifest group with
  no-clobber/manifest-last local/remote semantics and 3600-second rechecked orphan grace.
- Modify: `scripts/restore-postgres.sh` — generic manifest-bound validator/launcher for the shared owner maintenance
  executor, with stopped-writer recheck, read-only backup mount, independent writable `RESTORE_STATE_DIR`, and no
  0019 rollout-artifact parsing.
- Modify: `scripts/init-db-roles.sh` — all standalone calls acquire management/target lifecycle admission and check
  all three database catalog facts, refusing any active/`needs_attention` restore; expose grant SQL/functions for the
  existing holder without a second owner session or early CONNECT.
- Create: `backend/src/ai_employee/cli/verify_restored_backup.py` — manifest-revision-selected read-only checks that
  require an established owner maintenance connection already switched to `ai_employee_app`.
- Create: `scripts/convert-legacy-backup.sh` — development/test-only unique-project isolated conversion to a new
  ordinary manifest-bound group, with a pre-resource durable registry and exact resource labels.
- Create: `scripts/scavenge-legacy-backups.sh` — grace/attempt-lock/label-verified orphan cleanup after SIGKILL,
  Docker daemon failure, or host crash.
- Create: `scripts/test-legacy-backup-conversion.sh` — exact recipe/Compose isolation, crash scavenging, active-run
  preservation, and cleanup contract.
- Create: `scripts/restore-calendar-aad-0018.sh` — sealed owner-before-secret artifact/image guard and exact
  revision-0018 restore.
- Modify: `justfiles/ops.just` — resolve generic backup image metadata; keep manifest-bound `just restore file`
  independent with pre-owner Compose/write-switch checks and second confirmation; invoke the shared maintenance
  executor for generic/sealed restore; expose exact `restore-legacy-to-isolated file output_basename` and
  `legacy-backup-scavenge`; add no-argument preflight/migration/resync/audit recipes and
  `just calendar-aad-restore-0018 file`.
- Modify: `justfiles/db.just` — keep lifecycle-wrapped reset/drop/create/migration and reject active restore-call
  authority after holder crash; direct `dropdb`/`createdb` remain forbidden.
- Modify: `scripts/test-tooling.sh` — generic global/local lock, revision CAS, unique staging, three-file publication/
  rclone/retention/orphan/group-conflict validation; stopped-write/management-target locks, three database facts,
  state-v4/read-only-mount/psql-backend-registration/SQL-byte-zero/deferred-guard stream/ordinal phases,
  lifecycle-wrapped reset, legacy registry and scavenger gates;
  backup-basename/versioned-rollout-digest/image
  validation, durable refresh lease/fence/CAS preflight, guarded migration/audit/resync commands, zero-owner-call
  sealed image/script guard, exact production confirmations, shared maintenance executor and operation-specific
  owner-session role-switched verifier regressions.
- Modify: `scripts/test-deployment.sh` — statically prove generic owner maintenance, isolated legacy database/
  conversion, sealed owner boundaries, image-internal executable paths/digests, no executable bind mounts,
  dependencies, roles, Secrets, volumes, fixed atomic flags, no-`backend-common`/migration inheritance, plus sealed
  exact-image/no-pull contracts.
- Modify: `backend/tests/unit/test_init_db_roles_script.py` — grant bootstrap runs only after successful owner restore and remains Secret-file-only.
- Modify: `backend/tests/integration/retention/test_role_permissions.py` — app-role restore/DDL denial, active-gate
  standalone bootstrap rejection, holder-session grants without early CONNECT, owner-session
  `SET ROLE ai_employee_app` plus `BEGIN READ ONLY` DML rejection, and atomic app/retention CONNECT reopening with
  PUBLIC revoked.
- Create: `backend/tests/unit/infrastructure/db/test_postgres_restore_stream.py` — deterministic process harness for
  spawn-before-visible and ready-before-feed zero-SQL behavior, bounded forwarding, missing-trailer rollback,
  generator/consumer failure, FD closure, redaction, and exact status classification.

Tasks 27A–27E reuse the `audit_events` table and `oauth_connections.authorization_generation` already present in
revision 0018. It must not add a 0018 migration, an OAuth-only service, a new Task 18 command, or any fifth external
write action merely to implement the refresh fence.

##### Detailed 27E retention and deletion-barrier RED contract

~~~python
async def test_mail_body_retention_clears_all_aead_material_but_keeps_hash() -> None:
    await retention.run(now=RETAIN_AFTER)

    version = await load_mail_version(VERSION_ID)
    approval = await load_approval(APPROVAL_ID)
    assert (version.body_ciphertext, version.body_nonce, version.body_key_version) == (None, None, None)
    assert (approval.payload_ciphertext, approval.payload_nonce, approval.payload_key_version) == (None, None, None)
    assert approval.payload_hash == ORIGINAL_HASH


async def test_calendar_event_retention_clears_each_field_four_column_group() -> None:
    await retention.run(now=RETAIN_AFTER)

    event = await load_calendar_event(EVENT_ID)
    assert (
        event.description_ciphertext,
        event.description_nonce,
        event.description_key_version,
        event.description_aad_version,
    ) == (None, None, None, None)
    assert (
        event.location_ciphertext,
        event.location_nonce,
        event.location_key_version,
        event.location_aad_version,
    ) == (None, None, None, None)


async def test_unresolved_refresh_fence_survives_audit_cutoff_and_still_blocks_replay() -> None:
    await seed_refresh_started(created_at=OLDER_THAN_365_DAYS, user_id=USER_ID)
    await retention.run(now=AFTER_NORMAL_AUDIT_CUTOFF)

    assert await load_refresh_started(user_id=USER_ID) is not None
    result = await run_preflight_again(basename=DIFFERENT_BASENAME)
    assert result.error_code == "calendar_aad_refresh_fence_unresolved"
    assert fake_oauth.provider_calls == 0


async def test_all_data_deletion_reconciles_claimed_write_then_removes_local_provider_ids() -> None:
    await deletion.execute(user_id=USER_ID, request_id=REQUEST_ID)

    assert fake_provider.reconcile_calls == 1
    deleted_user = await load_user(USER_ID)
    assert deleted_user.is_active is False
    assert deleted_user.email == f"deleted-{USER_ID}@invalid.local"
    assert await provider_resource_ids_for(USER_ID) == ()
~~~

Cover 30-day mail body retention, 180-day metadata, event-end-plus-180-day calendar snapshots, 365-day history, deletion barriers, and retention-role permissions. Add a cutoff matrix proving an unresolved `oauth.refresh_started` survives beyond the normal audit deadline and still makes the next same/different-basename invocation return with `provider_calls == 0`; another user's fence and ordinary audit rows remain user-isolated and obey their own cutoff. Matching `oauth.refresh_confirmed` closes only its own automatic started. The cleanup path must reuse the exact versioned result-union parser used by reconcile: confirmed is legal only when missing/same means old == new plus `refresh_identity_changed=false`, or different means old != new plus changed=true; replacement is legal only with old != new plus changed=true. Reject every disposition/flag/equality mismatch. An exact `oauth.refresh_credential_replaced` may close an older unresolved fence only when it binds the original automatic attempt/started source/connection digest/F, both original pre-digests, old identity/fixed key version, the recovery OAuthAttempt and recovery-started event, valid F/S/T with `pre_generation=post_generation=T`, both required post-digests, different-token new identity/version, `refresh_identity_changed=true`, persisted expiry, stable result code, and strict event ordering. `oauth.refresh_recovery_unsatisfied` closes only its recovery attempt, so multiple unsatisfied pairs leave the original fence unresolved; its deletable group must match the versioned result schema, recovery event/OAuthAttempt, F/S/T, canonical requested capabilities, `capability_transition`, and stable error/result codes without requiring the current capability state to remain unchanged. Consumption remains valid after later generation, capability/scope, access, or refresh changes; generation-only, either physical digest alone, same-plaintext re-encryption, automatic different-token rotation, and access-only rotation must not release it. Retention never compares current credential lineage. For every deletable group, assert each event is older than cutoff—equivalently `max(created_at) < cutoff`; an old started paired with a newer confirmed/unsatisfied/consumption remains. Race retention against confirmed/recovery-result/consumption CAS and preserve the anonymized M1 user row.
Also seed a valid completed `restore-call:v4` plus
`ai_employee.restore_completion=restore_completion:v1:<digest>` zero-slot-bound pair with a matching old
`database.restore.completed` audit. The audit must be deleted by the ordinary 365-day cutoff while both catalog facts
remain unchanged, and a fresh admission/ACK check must still classify completed from the pair + gate absence +
baseline ACL. All-data deletion must likewise remove the user's completion audit without clearing either database-wide
catalog fact, preserve exactly one inactive anonymized `users` row, and allow a later final restore transaction to
attach its new exact audit to that sole row. AuditEvent BigInteger IDs, row existence/count, event/schema/metadata,
and malformed audit content never participate in admission/ACK authority; malformed/missing/mismatched completed
catalog facts fail closed at the restore boundary, not in retention cleanup.
保留原有测试覆盖：邮件正文/元数据和日程内容 AEAD 清理、CalendarEvent 描述/地点四列原子清空、未决 reconciliation 在命令脱敏前转 `needs_attention`、过期内容禁止重新提交、source-cache 与独立草稿/提案的删除区别、未认领取消、已认领有界核对、用户写屏障、provider-neutral token 清理、删除审计最小化以及每张新增表的 retention-role 权限。还要证明普通过期 audit 不因 fence 例外被误保留，旧 started 与较新 confirmed/unsatisfied/replacement 不能提前删除，且清理不读取后续 credential lineage。
追加的 coordinator RED tests 必须覆盖：automatic started 尚未提交前 provider call 为零；同一 connection 双 Worker/Taskiq duplicate delivery 只有一个 provider call；provider response 后 connection lease 丢失、CAS miss、`TransientProviderError`、网络未知或数据库明确 rollback 留下 unresolved fence，后续 delivery 的 provider call 为零。result commit ACK 丢失时，新数据库 session 按 user/connection/attempt 查询 versioned `ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1` union：confirmed/replacement actual commit 永久关闭 automatic attempt，unsatisfied/replacement actual commit 永久关闭 recovery attempt，只有 replacement 消费 original fence；各 actual rollback 分支不补写 result、不重放 provider/code。parser RED cases 必须拒绝 confirmed disposition、`refresh_identity_changed`、old/new identity equality 任一不一致，以及 replacement 非 old != new、changed=true 的 metadata。再覆盖 ACK 丢失后另一合法 refresh/reauth 先提交：历史 closing result 仍有效并按 current facts readiness；changed=false confirmed 后的 access-only refresh 与同 plaintext refresh-token re-encryption 即使完整物理 snapshot 变化也不冲突；changed=true confirmed/replacement 后 current identity 回到 old 的 A→B→A、缺行、AEAD/归属无效或其他不一致返回 `oauth_credential_state_conflict`且 provider calls 为零。preflight 与所有 Google/Microsoft mail/calendar automatic refresh 都经过同一 `OAuthRefreshCoordinator`，不会有适配器自有 retry 或第二个 upsert boundary。known-valid missing/same/different response 都写完整 confirmed，Google access-only 也 confirmed；missing/same 为 old == new、changed=false，different 为 old != new、changed=true，且 different token 不消费任何旧 fence。explicit recovery 取得同一 lease但不创建第二个 automatic started；denial/missing/same/known failure 在 current `T` 上把 requested capabilities 从 `authorizing` 收敛为 `action_required` 并保留 actual scopes/last-verified facts，stale `T` no-op；unsatisfied 无 credential post/expiry并保留原 fence，用户创建第二个 OAuthAttempt 后 different token 才以 old != new、changed=true proof 消费。Google/Microsoft 任意合法 error callback 一次性消费 state并统一执行脱敏/replay 不变量，但 Problem/error code 必须按安全分类矩阵断言：用户拒绝与未知名称为 `oauth_authorization_failed`，Microsoft consent evidence 为 `microsoft_admin_consent_required`，普通 `interaction_required` 为 `microsoft_reauthorization_required`；raw error 不记录也不作为持久 error code。shape 错误消费前拒绝，同一 state 未知-error replay 拒绝；targetless callback 命中 fenced existing identity 时在任何 local credential/scope/capability 保存前 fail closed。identity RED tests 先用独立标准库实现固定 HKDF/HMAC vector，再覆盖同一固定 APP root-key/version 的 API/Worker/CLI 构造一致性、same-plaintext re-encryption 保持 identity/changed=false 与 changed=true A→B→A rollback，以及解密/UTF-8 与边界验证/identity/started/lease/provider 的固定顺序；M2 只测试 immutable-key 行为，不测试多 key 生命周期，expected derived key/message/HMAC 不得由 production helper 生成，任何真实 token/HMAC key 都不得进入输出。

##### Detailed 27E retention RED evidence

Run: `TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py -q`

Expected: FAIL because M2 tables and encrypted command groups are not included in cleanup/deletion, CalendarEvent retention does not yet prove that each description/location AAD version is cleared atomically with its ciphertext, nonce, and key version, generic AuditEvent cleanup still deletes unresolved refresh fences incorrectly, or completion audits are still exempted from ordinary retention/privacy deletion.

##### Detailed 27E content-specific retention and source-cache implementation

Clear each existing three-column AEAD group atomically. CalendarEvent description and location are
independent four-column groups: for each field, clear `ciphertext`, `nonce`, `key_version`, and
`aad_version` together in the same transaction. Never leave a version orphan, clear only the old
triple, or temporarily violate the all-null/all-non-null field constraint. Apply the event-end-plus-180-day
deadline to these CalendarEvent content fields as well as compensation snapshots. Before clearing an encrypted command still attached to a
`reconciling` action, stop its automatic schedule and move it to `needs_attention` with
`action_content_expired`; manual resolution and provider links remain available, but no adapter can be
called without the authenticated command. A mail draft whose body expired becomes a content-free
historical record and cannot submit. Calendar snapshot retention is calculated from event end when
available, otherwise proposal retention. Source-mail deletion removes/cancels drafts bound to that
connection/thread only; source-calendar deletion does the same for proposals/snapshots. Independently
created drafts remain.

Replace the final generic bounded delete for `AuditEventModel` with a dedicated user-scoped cleanup query. It may
delete ordinary non-fence audit rows by cutoff, but the generic path must exclude exactly
`oauth.refresh_started`, `oauth.refresh_confirmed`, `oauth.refresh_recovery_authorization_started`,
`oauth.refresh_recovery_unsatisfied`, and `oauth.refresh_credential_replaced`. The dedicated path uses the same
versioned result union to classify only these closed, content-free metadata schemas and protects every unresolved
automatic started attempt. It must invoke the same strict parser as ACK-lost reconcile: confirmed missing/same is
valid only with old == new and `refresh_identity_changed=false`, confirmed different only with old != new and
changed=true, and replacement only with old != new and changed=true. Any disposition/flag/equality mismatch is not a
closing result and is not deletable.
For each candidate, explicitly filter `user_id`; lock owning connection, access credential row, refresh credential
row, and matching started audit in that order—confirmed, recovery-result, and replacement transactions use the same
order—then requery the exact event group. An automatic success group is started + confirmed. An unsuccessful
recovery group is recovery-started + unsatisfied and never closes the original automatic fence. The unsatisfied
event must use `oauth_refresh_recovery_unsatisfied.v1` and match the recovery event/`OAuthAttempt.id`, original
attempt/started source/connection digest, F/S/T, both frozen pre-digests, old identity/fixed key version, canonical
requested capabilities, stable error/result codes, target-`T` `action_required`-with-actual-scope-preservation or
stale-`T` no-op transition, and `created_at` strictly later than the recovery-started event; it has no credential
post/expiry fields. A successful recovery group is the original automatic started + matching recovery-started +
replacement consumption; the replacement closes both the recovery attempt and original fence and is valid only when it binds original attempt/started source/connection
digest/F, both original pre-digests, old identity
and fixed key version, recovery OAuthAttempt/event IDs, valid `F <= S` and `T=S+1` with
`pre_generation=post_generation=T`, both required post-digests, different-token new identity/version, persisted
expiry, `refresh_identity_changed=true`, stable result code, and `created_at` strictly later than both matching started
events. Automatic
`provider_refresh`, targetless
callback, physical snapshot/generation changes, same-plaintext re-encryption, access-only changes, or an unsatisfied
recovery never release the original fence. Once replacement proof is valid, do not compare later connection
generation or refresh snapshot and do not retain later credential lineage merely for cleanup. Candidate parsing must
never decrypt or load token/content fields. Delete only a complete group whose every event is older than cutoff,
equivalently `max(created_at) < cutoff`; never delete a newer result because its started is old, and never delete a
result first and strand a false unresolved started. M2 uses one fixed APP root key/version, so cleanup has no
historical identity-key retention branch. The exception must neither retain unrelated audit history nor cross user
boundaries.

Keep `database.restore.completed` inside the ordinary user-scoped cutoff delete. Retention must not read
`ai_employee.restore_call_authority`/`ai_employee.restore_completion`, invoke restore admission/ACK parsers, count
matching audits, or preserve completion
rows based on event/schema/metadata/digest. A valid, malformed, missing, or superseded completion GUC has no bearing
on whether an old completion audit is eligible; catalog validation belongs exclusively to restore admission/ACK.
All-data deletion must remove completion audits with the user's other AuditEvent rows and must not add a privacy
exception. The database-wide completed call + completion GUC pair continues to authorize completed independently of
the audit, while M1's single inactive anonymized `users` row remains available for a future final transaction's
required `user_id`. AuditEvent's ordinary
BigInteger ID and content never become restore authority.

##### Detailed 27E all-data write barrier and bounded final reconciliation

Before deleting rows, set the existing user active flag to false as the durable user-scoped write
barrier and make the claim transaction reject inactive users. Invalidate every unclaimed action. For
claimed/executing/reconciling actions, run one bounded read-only reconcile pass; regardless of result,
remove local tokens, command/body/snapshot ciphertext, provider IDs, drafts, proposals, and tasks,
then reuse M1's inactive anonymized user row and content-free deletion audit.

Refactor the privacy Worker from its Google-only revoker to the fixed provider registry. Hold at most
one selected token per connection in controlled memory, delete every provider credential locally,
then make one best-effort provider-specific revoke attempt. A provider without a safe delegated revoke
endpoint records the token-free maintenance fact defined in Task 25; local deletion must still finish.

##### Detailed 27A–27D preflight, recovery, deadline, and restore RED matrix

In `backend/tests/integration/operations/test_calendar_aad_0019_preflight.py`, upgrade the Task 13 synthetic
database to 0018 and seed complete historical description/location triples across two connections sharing an
opaque `calendar_id`, plus an unaffected empty-field calendar. Seed the exact non-`directory` cursors, connected
owning connections, enabled `calendar.read` rows, required access and refresh credential AEAD rows, exact
`ProviderCalendar` rows, and a directory cursor. Use Fake readers plus the real Google/Microsoft read adapters
behind HTTP mocks; never configure or contact a live provider.

Together, the preflight test, connection use-case/repository tests, and Google/Microsoft OAuth-flow tests must prove
all of these contracts:

- the no-argument scan requires Alembic revision `20260809_0018`, rejects partial historical triples, derives
  affected pairs only from complete triples, and never accepts an operator-supplied user/connection/calendar;
- before reading or checking any rollout artifact or making any provider call, the CLI opens a dedicated database
  connection and executes session-level `pg_try_advisory_lock(20260809, 19)`. The fixed keys encode the target
  revision and never include `BACKUP_ARTIFACT_BASENAME`; same-basename and different-basename preflights therefore
  share one global mutex. A losing try-lock returns exactly `calendar_aad_rollout_locked`, performs zero
  OAuth/Calendar calls and zero persistent writes, and publishes no artifact;
- the lease connection remains open from successful try-lock through all preflight network work and final artifact
  publication, but no business transaction remains open across network I/O. Before every provider call,
  credential commit, and artifact publish, the same session must prove connection liveness and ownership of the
  fixed advisory lock without reentrant reacquisition. Inject connection drop and explicit lock loss at each next
  boundary and assert fail-closed behavior; process/connection exit relies on PostgreSQL automatic release;
- every automatic existing-connection caller first uses the shared `OAuthRefreshCoordinator` to acquire a
  connection-scoped session advisory lease. The preflight holds both revision-global and connection leases before
  provider access; Google/Microsoft mail/calendar Workers use the same automatic application port. Run two
  Workers/deliveries against one connection and assert exactly one provider call. Inject response-after-lease-loss,
  network unknown, CAS miss and a database-confirmed rollback; each leaves a stable unresolved fence/needs-attention
  result, and the next Taskiq/`TransientProviderError` delivery performs zero provider calls. Separately inject a
  lost commit ACK for each result-union member. A new database session keyed by
  `user_id + connection_id + attempt_id` must discriminate `ConfirmedV1 | RecoveryUnsatisfiedV1 |
  CredentialReplacedV1`: actual commit permanently closes the member's allowed automatic/recovery started, while
  actual rollback leaves that started unresolved and never synthesizes a result. Unsatisfied closes only recovery;
  replacement also consumes original automatic. Before closure, confirmed must satisfy missing/same ⇒ old == new and
  `refresh_identity_changed=false`, different ⇒ old != new and changed=true; replacement must satisfy old != new and
  changed=true. Every mismatch is rejected as no legal result. Reconcile never adds a provider/code call. Loss of the original
  connection/session lease cannot block this PostgreSQL reconcile. After an actual commit, race a later legitimate
  refresh/reauthorization before reconcile and prove the old attempt remains closed while current readiness uses the
  new facts. A changed=false confirmed remains ready after a later access-only refresh or same-plaintext refresh-token
  re-encryption even if the complete physical snapshot changed. Only changed=true followed by current identity equal
  to old is an A→B→A rollback; that case, missing credential rows, invalid AEAD/ownership, or another inconsistent
  current state returns `oauth_credential_state_conflict`, blocks rollout, and keeps later `provider_calls == 0`.
  Explicit progressive recovery
  obtains the same connection lease through a distinct one-time authorization-code path and does not create a
  second automatic started. No adapter-owned retry, `ensure_connection`, unconditional upsert, or second credential
  writer is reachable;
- before any automatic provider refresh, a short transaction commits an existing append-only `AuditEvent` with
  `event_type="oauth.refresh_started"`, `task_id=NULL`, and `actor_type="system"`. Its metadata contains exactly
  `fence_schema_version="oauth_refresh_fence.v1"`, canonical `refresh_attempt_id`, `source`, applicable
  source/target revision and `rollout_digest_v1` (canonical NULL for ordinary Worker refresh),
  `connection_digest`, `fence_generation=G`, both pre-digests, old `refresh_token_identity_v1`, the fixed
  `refresh_token_identity_key_version`, and a stable result code—never token, raw scope, provider response, body,
  raw `calendar_id`, or credential timestamps. Local credential existence/ownership/AEAD-decryption checks must
  already have passed; access-only, missing, mismatched, or undecryptable refresh credentials produce neither
  started nor provider call. Assert provider calls remain zero until the started transaction commits and that no
  0018 migration or new table is introduced for the fence;
- fence/result lookup is durable across process restarts and independent of basename. The read-only boundary returns
  a versioned discriminated union: automatic started accepts only confirmed/replacement, recovery authorization
  started accepts only unsatisfied/replacement. A legal append-only member closes its matching attempt permanently
  based on schema/attempt/source/time ordering and the identity-change relationships above, without comparing mutable
  current rows. The same parser is reused by retention. An unresolved
  `oauth.refresh_started` blocks the same and different basename and all automatic Worker refresh before provider
access. `oauth.refresh_confirmed` must match the same attempt/source/started source and connection digest,
revision/rollout tuple, G/G/G, both
pre-digests, both required post-digests, old/new identity and fixed key version, persisted expiry,
  missing/same/different disposition, `refresh_identity_changed`, stable result code, and the 0019 deadline candidate;
  missing/same is old == new/changed=false and different is old != new/changed=true. Its `created_at` is
  strictly later than started. A matching `oauth.refresh_credential_replaced` permanently consumes an older fence
only when it binds the original attempt/started source/connection digest/F, both original pre-digests, old
identity/version,
  recovery OAuthAttempt/event IDs, `F <= S`, `T=S+1`, `pre_generation=post_generation=T`, both required
  post-digests, different-token new identity/version, `refresh_identity_changed=true`, persisted expiry, stable result
  code, old != new, and `created_at`
  strictly later than both started events. It closes both the recovery started and original fence. Preflight validates
  closure independently from later readiness and recomputes current plaintext identity using the fixed APP root
  key/version; an exact historical post-state resumes directly, while a later legal refresh/reauth uses current facts.
  For a changed=false confirmed, current identity equal to old == new after access-only refresh or same-plaintext
  re-encryption is normal even when physical digest differs. For a changed=true result, current identity equal to old
  is A→B→A and yields `oauth_credential_state_conflict`; missing required rows, invalid AEAD/ownership, or inconsistent
  current facts always yield the same conflict. None reopens or recalls the old attempt.
  Basename, generation, either physical digest,
  ciphertext/nonce/`updated_at`, access-only rotation, automatic different-token rotation, or an operator waiver
  cannot bypass the fence;
- Google and Microsoft progressive recovery start only on the original connection while it remains `connected`.
  Under the connection lock it loads `source_generation=S`, current refresh snapshot/identity, and exactly one
  unresolved automatic fence; it requires `S >= F` and current identity equal to the fence old identity. The
  existing `set_capabilities_authorizing` performs the only increment to `target_generation=T=S+1` and sets the
  requested capabilities to `authorizing`. In the same
  transaction create the existing target-`T` `OAuthAttempt` and append
  `oauth.refresh_recovery_authorization_started` with exact recovery schema/source, OAuthAttempt ID, connection
  digest, original refresh attempt/started source, F/S/T, both frozen pre-digests, old identity/fixed key version,
  stable result code, and `created_at` strictly later than the original automatic started. Cover `F=S`, `S>F`,
  zero/multiple fences, start-time identity mismatch, and atomic rollback of the generation/OAuthAttempt/event;
- targetless callback tests require that a normalized identity resolving to an existing connected row with an
  unresolved fence returns `oauth_refresh_recovery_requires_connection_start` before saving any credential, scope,
  or capability. It may append only the optional content-free blocked audit; it creates no candidate snapshot,
  target generation, replacement consumption, or second connection, and never reaches `ensure_connection` or an
  unconditional upsert;
- Google and Microsoft API tests require the callback to accept exactly `code+state` or bounded non-empty
  `error+state`. Missing state, code/error together, neither value, or malformed error rejects before state
  consumption. Every valid error, including an unknown name, consumes state once through `callback_error`; a
  target-bound attempt appends the versioned unsatisfied result/transition. Consumption, convergence, redaction, and
  replay behavior are common, but the stable Problem/error code follows the safe classification matrix: denial and
  unknown names use `oauth_authorization_failed`; Microsoft consent evidence uses
  `microsoft_admin_consent_required` with the existing administrator guidance; ordinary `interaction_required` uses
  `microsoft_reauthorization_required`. Raw error/description/codes never persist, become error codes, or log.
  Replaying that state with the same or another
  unknown error fails because state was consumed, and neither provider may bypass targetless pre-save blocking;
- authorization-code exchange remains outside the database transaction. The callback consumes the state/code once,
  resolves recovery linkage by `OAuthAttempt.id`, enforces
  `connection.authorization_generation == OAuthAttempt.target_authorization_generation == T`, obtains the same
  connection lease, and rechecks the original fence plus frozen snapshot/identity before exchange. It decrypts old
  refresh plaintext only in controlled memory and uses `secrets.compare_digest`. Only an explicitly returned
  non-empty different refresh token may, in one transaction, CAS the exact target-`T` state, persist access/refresh
  credentials, actual scopes and capability states, and append `oauth.refresh_credential_replaced`; generation
  remains `T`. The proof metadata is the complete closed set defined above, including both original pre-digests,
  both required post-digests, recovery OAuthAttempt/event IDs, old/new identity versions,
  `refresh_identity_changed=true`, old != new, persisted expiry and strict timestamps. Missing/empty/same refresh,
  user denial, admin-consent failure, safely known provider/network
  failure, or identity mismatch does not persist credential/scope/account facts or consume the original fence. When
  `connection.authorization_generation == T`, reuse/extend `mark_progressive_authorization_failed` so one short
  transaction changes only this attempt's requested capabilities from `authorizing` to `action_required`, records a
  stable error code, preserves existing actual scopes and last-verified facts, and appends
  `oauth.refresh_recovery_unsatisfied` with
  `result_schema_version="oauth_refresh_recovery_unsatisfied.v1"`, the matching recovery
  event/OAuthAttempt IDs, original attempt/started source/fence, F/S/T, both frozen pre-digests, old identity/fixed
  key version, canonical requested capabilities, `capability_transition="action_required"`, stable error/result
  codes, and `created_at` strictly later than recovery started; it contains no credential post snapshot/digest,
  new identity, persisted expiry, or deadline. If `T` is stale, capability mutation is a no-op and
  the safe audit uses `capability_transition="stale_target_noop"`; it cannot overwrite newer authorization state.
  Unsatisfied closes only that OAuthAttempt and preserves the original automatic fence; replacement closes that
  OAuthAttempt and consumes the original fence. Cover denial, missing, same,
  stale `T`, and creation of a new OAuthAttempt for the next attempt; the old code is never replayed. Lease loss,
  CAS miss, definite rollback and commit-ACK-loss follow the no-replay/result-union contract above. Test actual commit
  and rollback for both unsatisfied and replacement, including a later reauthorization winning before reconcile. Inject
  failure after every credential/scope/capability/event mutation and during commit;
- `application/ports/oauth_refresh.py` is the sole automatic provider-call admission and explicit recovery lease
  boundary, while `application/ports/credential_rotation.py` is the sole typed CAS/result boundary. API deps,
  mail Worker, calendar Worker, and preflight CLI construct AEAD and identity services from the same
  `APP_MASTER_KEY_FILE` bytes and fixed key version. All automatic callers carry generation plus both complete row
  snapshots and old identity/version; the shared repository locks connection → access row → refresh row → matching
  started audit and rechecks every field. Known-valid missing/same/different Worker responses all commit
  `oauth.refresh_confirmed`; missing/same preserves the refresh row byte-for-byte and records old == new plus
  changed=false, while different updates both rows and records old != new plus changed=true but never consumes an older
  fence. An unresolved predecessor prevents the automatic call entirely. Race mail
  versus calendar and duplicate Worker delivery; exactly one provider call/confirmed result may win. Race an
  explicit recovery callback against automatic entry and prove the unresolved fence keeps automatic calls at zero
  while at most one different-token recovery consumes it. After any closing result, later legal
  capability/generation/scope/credential changes do not revive the matching attempt; current readiness is evaluated
  separately. changed=false plus later access-only/same-plaintext re-encryption is non-conflicting; changed=true plus
  current identity equal to old is A→B→A. That rollback, missing rows, invalid AEAD/ownership, or invalid current facts
  returns `oauth_credential_state_conflict` with zero provider calls. In every case, the original D0 unknown preflight refresh
  call count remains unchanged.
- each pair must have the exact same-user cursor, owning connection in `connected`, `calendar.read` in `enabled`,
  both access and refresh AEAD credentials, and exact `ProviderCalendar`; disconnected, missing/mismatched,
  disabled, revoked, degraded, action-required, access-only, missing refresh, or refresh AEAD decryption failure
  fails before any event-scope provider access;
- affected pairs are grouped by connection UUID and processed in deterministic serial order. Only a connection
  with a committed new started fence and no unresolved predecessor may call the existing provider-neutral OAuth
  refresh using its decrypted refresh token. Assert no overlapping refresh calls and no second refresh for two
  calendars on one connection;
- validate the normalized refreshed `access_token`, positive bounded `expires_in`, optional rotated refresh token,
  and `granted_scopes`. The returned scopes must cover the connection's persisted canonical scopes and the
  provider's `calendar.read` requirement; refresh rejection/revocation, malformed values, unknown/scope-shrunk
  responses, or an expiry not later than `now + 900 seconds` fails before pair probes;
- before provider refresh, snapshot both access and refresh credential rows with exact `id`, `user_id`,
  `connection_id`, `credential_kind`, `ciphertext`, `nonce`, `key_version`, `token_expires_at`, and `updated_at`.
  After validation, the shared credential-rotation repository opens one short transaction, rechecks the owning
  connection, exact `authorization_generation`, and `calendar.read` capability; conditionally matches both old
  snapshots; writes access-token AEAD/expiry and optional rotated refresh-token AEAD; and appends
  `oauth.refresh_confirmed` with the same attempt/source, revision/rollout tuple, G/G/G, both pre-digests, required
  `post_credential_snapshot_digest_v1`, required `post_refresh_credential_snapshot_digest_v1`, old/new identity and
  fixed key version, exact persisted `token_expires_at`, missing/same/different disposition, required
  `refresh_identity_changed`, `rollout_deadline_candidate = token_expires_at - 900 seconds`, a stable result code, and
  `created_at` strictly later
  than started. Credential mutation without confirmed, or confirmed without credential mutation, must be impossible.
  When the provider omits a new refresh token or returns the same plaintext, prove the old refresh row is unchanged
  byte-for-byte and old == new/changed=false; when it returns a different non-empty token, prove both rows change under
  CAS and old != new/changed=true but no
  `oauth.refresh_credential_replaced` is appended. Existing unconditional credential upsert/
  `rotate_access_token()` semantics are removed rather than retained as a second writer;
- a credential CAS miss, connection/generation/capability change, post-provider lease loss, or database-confirmed
  transaction rollback cannot overwrite a concurrently refreshed token, prevents every later pair probe/artifact
  publication, leaves Alembic at 0018, and leaves a durable unresolved fence that blocks every later basename from
  calling the provider. A commit ACK loss is not equivalent to rollback: create a fresh session and read only the
  exact user/connection/attempt versioned result. If a legal matching result actually committed, it permanently closes
  its allowed attempt and no call is added; if it actually rolled back, no result is backfilled and every later
  provider/code call remains zero. Inject confirmed, unsatisfied, and replacement actual commit/rollback, network
  unknown result, `invalid_grant`, malformed response, scope shrink, lock loss, a competing explicit recovery/Worker
  entry, and CAS failure. Then inject a later valid refresh/reauth before ACK reconcile. For changed=false, test both a
  later access-only refresh and same-plaintext re-encryption with a changed full physical snapshot and no conflict. For
  changed=true, test A→B→A current old-identity rollback; also test missing-row and invalid AEAD/ownership conflict.
  Assert closure never depends on historical-post/current equality, conflict is `oauth_credential_state_conflict`, and
  no path guesses a result or invokes provider again;
- if credential CAS plus `oauth.refresh_confirmed` commits and the process crashes before rollout artifact publication,
  rerunning the same rollout recognizes the closing result. Exact post-state resumes from persisted token/expiry;
  later valid credential state resumes from current token/expiry after independent readiness. Both publish only when
  valid and never call the closed refresh attempt again;
- the digest unit tests independently decode the three fixed base64 canonical-byte vectors from design section
  17.3, assert the production canonicalizer returns those exact `497`-, `265`-, and `222`-byte values, and compare
  production digests with fixed lowercase SHA-256 constants
  `607c4c12cb3e4ff7736cb56f06b9dd2f63d2681c8d7a36c7801160eaafcaa84a`,
  `3a4b1dbd0e77c915067c3af352772580949957815573c953baa41131827582f0`, and
  `0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5`. Expected bytes and hashes must never be
  produced by `calendar_aad_digests.py` itself. Additional cases distinguish NULL from empty bytes and reject
  non-canonical UUID, integer, UTC timestamp, image ID, field order, or silent v1 schema changes;
- the identity unit test independently derives the fixed HKDF-SHA256 fingerprint key and 73-byte framed HMAC message,
  then compares the fixed `refresh_token_identity_v1` constant from design section 17.3 without calling production
  helpers. It proves API deps, mail Worker, calendar Worker and preflight CLI construct the helper from the same fixed
  `APP_MASTER_KEY_FILE` bytes and `AeadCipher.key_version`; missing or changed root key/version fails closed. It also
  proves same-plaintext re-encryption preserves identity and maps to changed=false rather than rotation, covers
  changed=true A→B→A rollback, and proves the runtime order is exact-AAD decrypt in controlled
  memory, non-empty/UTF-8/bound validation, identity calculation, committed started/lease checks, then provider access.
  It proves no plaintext/root-key/derived-key output. M2 has no alternate-key lifecycle case;
- each valid pair calls only `initial_pages(scope_key)` and must obtain a final cursor; `directory_pages()`, a
  connection-level owner, every other pair, and all provider write adapters receive zero calls;
- the probe consumes the same bounded event-scope page contract as post-migration recovery but persists no
  CalendarEvent, SyncCursor, marker, TaskRun, ApprovalRequest, or ToolExecution change;
- probes use only the newly refreshed access token. A resource 401 after proactive refresh fails immediately and
  does not call OAuth refresh again. No other local mutation—including connection/capability status changes—is
  allowed; 401, 403, revoked scope, malformed pages, or missing final cursor fails the whole preflight;
- use an injected UTC clock and fixed constant `CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS = 900`. Derive
  `rollout_deadline` from the minimum persisted refreshed `token_expires_at`, never from a configurable timeout or
  operator estimate. Assert preflight fails if any connection expiry cannot provide the margin, if a pair probe or
  final artifact commit crosses the current-row `effective_deadline`, and that multiple connections choose the
  earliest expiry; historical confirmed deadline candidates never replace that current-row read;
- zero affected pairs is a content-free success, while any failed pair returns nonzero. Output and test artifacts
  contain only schema/revision, `rollout_digest_v1`, the safe backup basename, the host-resolved immutable image content ID, earliest
  expiry/deadline or an explicit zero/no-deadline sentinel, fixed margin, affected/connection counts, hashed
  connection IDs, stable error codes, and the same lowercase SHA-256 pair digest later used by recovery:
  `20260809_0019 + NUL + connection_id + NUL + calendar_id`. They never contain raw `calendar_id`, token, body,
  scope strings, description, location, or provider response; the artifact mode is `0600`, its safe basename is
  bound to `BACKUP_ARTIFACT_BASENAME`, and its immutable image content ID comes only from host-side inspection of
  the Compose-selected backend image rather than an operator-supplied value. Every later one-off must resolve the
  actual image again and reject a mismatch.
- run concurrent same-basename and different-basename preflights against the same PostgreSQL database; in both
  cases only the lease winner may enter the provider, and its Fake refresh count is exactly once per distinct
  affected connection. The loser remains zero-write even if its artifact path would otherwise be available.

In `backend/tests/integration/operations/test_calendar_aad_0019_recovery.py`, use the Task 13 synthetic
PostgreSQL database and Fake Google/Microsoft readers. Seed two users and two connections that share the
same opaque `calendar_id`; only one exact cursor carries
`last_error_code="calendar_event_resync_required"`. Also seed a marked second calendar, an unmarked
calendar, a `directory` cursor, and an unrelated ordinary `sync_calendar` TaskRun/Outbox fact.

The application/repository tests must prove all of these contracts:

- the scan requires Alembic revision `20260809_0019` and selects only existing non-`directory`
  `resource_kind="calendar"` cursors with the exact marker;
- the connection row is the source of `user_id`; cross-user task input, a missing/disconnected connection,
  missing/non-enabled `calendar.read`, missing access or refresh credential, missing exact ProviderCalendar,
  missing cursor, cleared marker, or `scope_key="directory"` fails before provider access;
- each selected pair creates one task of kind `calendar.aad_0019.resync` with the exact input keys
  `{"connection_id", "scope_key", "recovery_revision", "pair_digest", "recovery_attempt_ordinal"}`,
  fixed revision `20260809_0019`, recomputable digest, and first ordinal 1;
- TaskRun, content-free `task.created` audit, and initial `task.execute` Outbox are committed together;
  an injected persistence failure leaves none of the three facts;
- the pair digest is lowercase SHA-256 of revision, connection ID, and exact calendar ID separated by NUL; the
  user-scoped idempotency key is `calendar-aad-0019:<pair_digest>:attempt:<ordinal>` and stores no raw calendar ID;
- the planner locks the exact cursor row with `FOR UPDATE` or equivalent CAS. `created`, `queued`, `running`, and
  `retry_scheduled` are active and must always return/reuse that same ordinal without allocating another; two
  concurrent planners create only one TaskRun/initial Outbox for the same new ordinal, while multiple active
  attempts fail closed;
- if the latest attempt is `failed`, repairing the Fake/provider condition and invoking the CLI again creates
  ordinal 2 and succeeds; `cancelled` likewise permits a later new ordinal. The original terminal TaskRun,
  timestamps, result, error, and `attempt_count` remain byte-for-byte unchanged;
- `succeeded` with a surviving marker, or `waiting_approval`, `reconciling`, or `needs_attention` for this task
  kind, is an invariant failure. A single CLI invocation plans each pair once and cannot create ordinal+1 after a
  task it just ran fails; TaskRun `attempt_count` continues to cover only the existing runner's bounded retries;
- the one-off executor dispatches and runs only the task IDs returned by the recovery planner; the
  unrelated ordinary TaskRun/Outbox remains in its original state and no general queue scan occurs;
- before credential or provider access, the Worker recomputes the digest and verifies exact input keys, positive
  ordinal, idempotency key, revision, user ownership, marker, cursor, capability, both access/refresh credentials,
  and ProviderCalendar;
- the marked pair makes exactly one bounded event-scope read, `directory_pages()` is never called, the
  same-ID calendar on the other connection and every unmarked provider/calendar receive zero calls, and
  no connection-level directory owner is invoked;
- success writes/decrypts only v2 description/location groups, restores the exact cursor/freshness, and
  clears only that pair's marker; marker-clear replay is an idempotent no-op with zero provider calls;
- provider/read, lease, or final marker-CAS failure leaves the marker in place and never broadens to a
  full connection/account sync; and
- no path creates an ApprovalRequest, ToolExecution, trusted command, or provider write call. CLI output,
  logs, audit metadata, and test artifacts must not contain raw `calendar_id`, token, body, description,
  or location values.

In `backend/tests/integration/operations/test_postgres_backup_restore.py`, use the Task 13 synthetic PostgreSQL
database, real lock sessions, and the encrypted backup/restore scripts behind safe local process fakes where remote
storage is involved. Cover all of these cases:

- every backup/migration/restore/reset wrapper first derives the fixed low/high-bit target digest vectors and obtains the
  cluster/target lifecycle lock from management database `postgres`. Lock ordering is management→target→schema;
  restore holds management lock through attempt/reconcile/reopen and reset holds it through
  check→drop→create→migration. Simulate holder crash plus another-host reset: active/`needs_attention` catalog
  authority keeps drop/create/migration at zero even after the advisory lock is released;
- the same target database serializes same/different basename backup, remote, retention, and orphan runs through
  `BACKUP_LIFECYCLE_LOCK=(20260806, 274)`, including callers with distinct simulated hosts/BACKUP_DIR values; the
  same `${BACKUP_DIR}` additionally serializes through `.ai-employee-backup.lock` `flock`;
- every run owns a unique `.partial.<uuid>` local tree and remote staging prefix. Same-basename conflict, any
  generation/rename/upload failure, cleanup racing a live run, crash before manifest, and remote conflict remove
  only current-run or expired revalidated orphan state. Fixed grace is 3600 seconds, and deletion rechecks mtime plus
  absence of final manifest while both locks remain held; another run, complete group, or recent group is untouched;
- backup takes shared `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`, reads pre-revision, holds it through complete
  `pg_dump`, reads equal post-revision, proves the same session still owns the lock, and only then publishes the
  manifest last. Launch every Task 16A migration entry while dump is active and prove its exclusive lock prevents
  commit. Simulate a bypassed revision change and shared-session loss; neither may publish any final member. The test
  does not require a shared MVCC snapshot;
- ordinary publication creates a `0600` dump/checksum/versioned-manifest group whose checksum binds dump plus final
  manifest and whose manifest binds checksum filename plus dump digest/size/revision/image metadata,
  `restore_fingerprint_v1`, and the allowlisted image-internal executable digest map. The test builds the actual
  `backend/Dockerfile` from repository-root context, proves every operations entrypoint is copied into the immutable
  image, and rejects executable bind mounts. no-clobber, rclone, retention, and cleanup all act on the full group;
- generic `just restore file` accepts a fully verified manifest-bound backup at any supported revision, including
  0019, without preflight/`pre-migration` artifacts. Before owner access it rejects any enabled write switch,
  Caddy/API/Worker/Scheduler/migration/role-bootstrap/general or owner consumer, missing second production
  confirmation, unsafe/partial/mismatched group, or pre-manifest production input with `pg_restore_calls == 0`;
  development/test also rejects same-workspace writers unless the target is isolated;
- every migration/reset/drop/create wrapper, role-bootstrap/`init-db-roles.sh`, generic/sealed restore/verifier,
  backup and owner one-off derives the same fixed keys and parses authoritative database-wide maintenance
  gate, restore-call authority, and restore-completion GUC from `pg_db_role_setting`. A live holder rejects on lock;
  a crashed holder leaves catalog authority visible on another host. Both cases produce zero lifecycle/DDL/DML/
  audit/grant/CONNECT/authority/psql/`pg_restore` calls for nonmatching callers; role-specific/duplicate/malformed
  phase, ordinal, backend/timeline/expected-observed fact, or completion fact fails. Freeze `restore-call:v4` as
  ASCII-only, exactly 21 delimiters and at most 1133 bytes, completion as exactly 86 ASCII bytes, and reject every
  malformed phase-dependent dash/value combination. Within one call ordinal reject clearing or rewriting frozen
  call-start/pre/backend/observed facts; across the legal new-ordinal starting edge require ordinal+1, fresh
  call-start, exact-current pre re-freeze, backend PID/start reset, observed/completed starting shape, and rejection
  of prior backend inheritance or ordinal reuse. Preserve the direct exact-post ordinal-0 shape;
- test the complete `pristine_idle → active → completed_idle → next active` matrix plus every off-matrix tuple.
  Pristine all-absent + baseline ACL permits migration/role-bootstrap. For manifest-bound generic/sealed, the holder
  validates/archives any prior completed pair, resets completion, atomically replaces it with the exact active call +
  gate and PUBLIC/app/retention CONNECT revocation, writes no pre-restore gate audit, terminates old non-owner
  sessions, rejects new app/retention connections, and keeps the lock-holding control session across the supervised
  restore, grants, verifier, and final transaction. Legacy uses only its durable registry/disposable target and never
  writes the workspace catalog trio;
- generic/sealed backup/artifact mounts are read-only and a distinct `RESTORE_STATE_DIR` is the only writable mount.
  `.env.example` supplies only the safe development/test host default; production requires an explicit absolute
  volume with `0700` directory/no-symlink/non-overlap checks. Its mode-`0600` state-v4 file is merely a projection of
  exact gate/call/completion facts; completed projection is an immutable prior-pair archive. A second host with no
  file rebuilds every phase/call/backend/completion fact from catalog, including the required completed call +
  completion pair. Missing/corrupt/stale projection never means “not spawned” and missing completed catalog call is
  corruption, not a valid completed state;
- the exact frozen phase graph and all legal edges pass, while terminal overwrite, jump, regression, self-transition,
  stale attempt/phase/call CAS and reopen-ordinal CAS fail. Grant rollback remains `restore_succeeded`; verifier
  failure remains `grants_succeeded`; `reopen_not_applied` is retried only with a new explicit `reopen_ordinal`;
- no-gate exact-post evidence produces only deterministic kind-aware no-clobber local evidence with database writes/audit/GUC/psql/
  pg_restore all zero. Matching-gate exact post takes the direct `restore_succeeded` edge without a separate audit,
  then still completes grants/verifier/reopen. Every real ordinal first proves exact pre-state, CASes
  `restore_backend_starting` with ordinal+1, a fresh call-start, exact-current pre re-freeze, backend PID/start reset
  and observed/completed starting shape, and starts a fixed-target credential-bearing psql consumer whose controller-owned pipe
  contains zero SQL. The holder registers exact PID/backend-start/database/role/`PGAPPNAME`, CASes ready, and only
  after started CAS plus v4 projection fsync sends the deferred guard and launches a credential-free pg_restore SQL
  generator. Cover spawn-before-visible crash, backend-ready-before-feed, SQL-byte-zero before started, mid-stream or
  missing-trailer rollback, generator/consumer failure, commit/rollback/partial results, stale PID/backend-start,
  two-host overlap, cross-host no projection where starting has no provable local child/pipe identity and therefore
  CASes `needs_attention` with `pg_restore_calls == 0`, the original-controller-only starting→ready path, and explicit
  new ordinal only from legal `restore_not_applied`; prove that this retry cannot inherit the prior PID/start and
  that the same ordinal's ready/started/outcome shapes cannot clear call-start/pre/backend facts;
- generic/sealed role grants after restore do not reopen CONNECT. The holder session runs `SET ROLE ai_employee_app`,
  `BEGIN READ ONLY`, asserts current/session user, receives SQLSTATE `25006` for DML, and completes every failing
  revision/fingerprint/data check before reopen. One final transaction requires exactly one active-or-inactive
  anonymized administrator row (zero/multiple rows fail closed), writes an ordinary BigInteger-ID completion audit with the fixed user/task/event/
  actor/database-time outer fields, safe gate/call timeline, expected/observed facts, and exact schema/metadata/
  authority digest, sets
  `ai_employee.restore_completion=restore_completion:v1:<digest>`, CASes
  call authority to completed, resets the gate, and restores only app/retention CONNECT while PUBLIC stays revoked.
  The digest is computed from the full completed `restore-call:v4` with a fixed 64-zero final slot. ACK-loss uses a
  new owner session to require the mutually bound completed call + expected completion GUC, gate absence, and baseline
  ACL: completed rebuilds only projection, audit retention/privacy deletion does not invalidate it, definite
  absent-GUC active rollback CASes to `reopen_not_applied`, and mixed facts enter `needs_attention`; no committed
  reopen repeats. Cover a second restore archiving the old completed pair before replacing it/resetting completion,
  and a crash immediately after restore commit;
- `just restore-legacy-to-isolated file output_basename` is rejected outside development/test. Valid input creates a
  fsynced registry before any resource, then a uniquely labeled Compose project, internal network, ephemeral volume,
  and `0600` registered Secret; restores only the isolated database; reads actual revision/health; and publishes the
  requested no-clobber output through the ordinary locked backup path. Normal trap cleanup and the separate
  fixed-grace/attempt-lock/label-aware scavenger cover success, failure, SIGKILL, daemon crash and host crash while
  preserving another active run. Unsafe/conflicting basename, bad checksum, unsupported revision, restore/health
  failure, and simulated crashes leave no new group or sensitive output and never touch workspace/production.

In `backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py`, use an injected UTC clock,
the Task 13 synthetic PostgreSQL database, and the real sealed encrypted backup/restore scripts behind safe local
process fakes where external storage is involved. Cover all of these cases:

- backup, each audit phase, the guarded migration, recovery planner, recovery Worker final CAS, and post-resync
  artifact each reload current access-credential expiry and reject `now >= effective_deadline` at start or
  immediately before their critical commit/publication. Historical confirmed deadline candidates only validate
  result schemas; a later shorter expiry tightens the window and a later longer expiry cannot extend it;
- the canonical zero/no-deadline state is accepted at those same boundaries only after an exact affected-set
  recheck remains empty; a forged zero state, later non-empty set, non-null zero-state timestamp, or actual image
  content ID mismatch fails closed;
- crossing the current-row `effective_deadline` never starts a general service, creates a later ordinal, publishes a partial artifact, or
  mutates a marker through a bypass path;
- a failed/cancelled attempt with time remaining does not force sealed restore and can allocate the next ordinal only on a
  later explicit CLI invocation; once the current-row `effective_deadline` is reached with a marker still present or timely artifact
  completion can no longer be proven, the exact encrypted dump created after active refresh and before migration
  restores the whole database only through `just calendar-aad-restore-0018` and the operations-profile
  `calendar-aad-restore-0018` service. Before any owner Secret read,
  owner database connection, decryption, or `pg_restore`, the host guard independently validates the safe basename,
  encrypted dump, adjacent checksum and versioned manifest, preflight and original `pre-migration` artifacts, their
  rollout/pair digests, the bound local immutable image, and the image-internal executable digest map. A moved tag,
  missing/wrong image/script, executable bind mount, checksum failure, or artifact/image mismatch fails
  with `owner_connection_calls == 0` and `pg_restore_calls == 0`;
- only after that guard succeeds may the recipe inject the matching exact `sha256:...` as internal
  `CALENDAR_AAD_RESTORE_IMAGE_ID`. Rendered Compose must prove the sealed restore service `image:` directly references
  that ID, has no tag or `build:`, and the invocation fixes `--pull never`; no pull/build may occur. The service
  must not inherit `backend-common`, depends only on healthy PostgreSQL, uses the bootstrap owner credential only in
  the fixed-target psql consumer; the pg_restore generator receives no `PG*`/DSN/Secret value or database
  credential. It
  mounts the backup passphrase, controlled backup volume and bound artifacts read-only, a separate writable
  `RESTORE_STATE_DIR`, and app/retention password Secrets. Its entrypoint rechecks artifact/image/script environment
  before reading `postgres_bootstrap_password`;
- after the sealed-only guard, `kind=sealed_0018` enters the same management/target/exclusive-schema locks, database
  gate/call/completion facts, state-v4 projection, psql backend starting/ready/started registration, deferred-guard
  credential-free pg_restore SQL streaming, authority-bound backend-exit reconcile, and ordinal-aware atomic reopen
  used by generic. A different manifest/generic attempt on the same target cannot enter. App-role restore remains
  denied; an injected mid-stream error or missing trailer rolls back with no partial state;
- sealed grants and the deterministic revision-0018/artifact verifier are callbacks on the same lock-holder owner
  session while CONNECT remains revoked. The verifier executes `SET ROLE ai_employee_app`, `BEGIN READ ONLY`, and
  proves SQLSTATE `25006` before comparing revision/pair/count/digest/cursor/ciphertext facts. All checks finish before
  one final completion-authority/GUC/fixed-outer-field ordinary-audit-ID/schema/metadata/digest/gate-reset/minimal-
  CONNECT transaction. ACK loss uses the same required-completed-call/completion-GUC/gate/ACL new-session predicate including
  `reopen_not_applied`; no standalone app-only verifier runs after reopen. The original 0018-compatible immutable
  image cannot start until the expected completion-GUC/gate-reset/ACL shape is proven; and
- tests assert zero calls to Alembic downgrade, direct SQL marker clearing, directory/full-account sync, temporary
  OAuth-only services, or provider writes. The fixture explicitly proves the sealed window has no business writes,
  so whole-database restore discards only attempted 0019 migration/resync facts and preserves the refresh rotation
  included in the backup.

Extend `scripts/test-tooling.sh` with a stubbed Compose boundary that proves
`just calendar-aad-preflight-0019`, `just calendar-aad-migrate-0019`,
and `just calendar-aad-resync-0019` accept no pair argument and refuse to run while `caddy`, `api`, `worker`, or
`scheduler` is running. `just calendar-aad-restore-0018 file` remains the only sealed restore/verifier entry. All
rollout commands require a safe
`BACKUP_ARTIFACT_BASENAME`; preflight creates the bound `0600` rollout state, while every later command receives
that exact file and cannot substitute an operator-entered deadline or image ID. Stub host-side image inspection and
prove each command passes the actual Compose-selected backend image content ID, while a missing image, `latest`,
inspect failure, moved tag, or artifact mismatch fails before database access. The host recipe validates only the
basename/path shape and mount boundary; artifact existence/collision checks occur inside the lease-owning CLI,
never before the revision-global try-lock. The preflight additionally requires revision 0018 and
overrides the `worker` service command with only the fixed Secret wrapper
`export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_preflight_0019`;
the migration recipe targets only 0019 and uses the fixed `ai_employee.cli.calendar_aad_migrate_0019` wrapper;
the recovery recipe requires revision 0019 and uses the equivalent fixed `ai_employee.cli.calendar_aad_0019`
module. The sealed verifier is an internal callback on the holder owner connection, requires restored revision 0018,
switches to app role, starts `BEGIN READ ONLY`, and proves the same connection cannot execute DML before atomic
reopen. None may start
`taskiq worker`, the Taskiq scheduler, a general Outbox/Redis queue consumer, or an OAuth-only service.

Extend the same tooling/deployment/role tests for both restore entries. Generic `just restore` invokes its owner
disaster-restore service, never `backup`, preserves exact path confirmation/production authorization, requires
checksum plus the fixed psql-consumer and credential-free pg_restore-generator flags, and does not inspect 0019 artifacts. It must show host stop checks,
target lock/database GUC establishment, PUBLIC/app/retention CONNECT revocation, existing-session termination,
post-check new-connection denial, fsynced state-v4 starting/ready/started intent, psql backend registration,
deferred-guard pg_restore SQL stream and unknown-outcome reconcile, holder-session grants/verifier, and
completion-GUC plus fixed-outer-field ordinary-ID exact metadata/digest/gate-reset/ACL atomic reopen with ACK-lost
reconciliation; an ordinary revision-0019 backup
restores successfully and `pg_restore_calls == 1` on the baseline path.

The separate legacy tooling test invokes only
`just restore-legacy-to-isolated file output_basename`, requires development/test, renders unique-project isolated
PostgreSQL/conversion services on an internal network with an ephemeral volume and registered `0600` Secret, and
proves pre-resource registry/labels, trap cleanup, SIGKILL/daemon/host-crash scavenging, active-run preservation, and
normal manifest-bound output. It cannot invoke generic target restore or sealed restore.

`just calendar-aad-restore-0018` invokes a different service/script. Its host stub proves
basename/dump/checksum/manifest/preflight/`pre-migration`/image validation all happen before any owner Secret or database
use, and moved tag, missing/wrong image, or artifact mismatch yields zero owner connection, zero `pg_restore`, zero
pull, and zero build calls. It also rejects missing/mismatched image-internal executable digests and executable bind
mounts. Rendered Compose proves the sealed service has only
`postgres: service_healthy`, no `backend-common`/`migration`/Redis dependency, an exact internally injected
`sha256:` `image:`, no `build:`, fixed `--pull never`, bootstrap owner Secret visible only to the controller, bound
artifact plus backup passphrase/read-only backup mounts, and app/retention Secrets
needed by the holder-session grant callback. No service-wide `PGUSER=ai_employee_owner`/`PGPASSWORD` is allowed;
process-level tests prove only the fixed-target psql consumer receives the bootstrap owner credential, the
credential-free pg_restore generator receives no `PG*`/DSN/Secret material, and neither process leaks credentials or
SQL to argv/log/evidence. Role integration proves app-role restore denial, target lock/gate sharing with generic,
exact psql PID/backend-start/`PGAPPNAME` registration, zero SQL before started, deferred-guard rollback on crash or
injected child error, atomic owner success, restored grants, holder-session
`SET ROLE ai_employee_app` + `BEGIN READ ONLY`, SQLSTATE `25006`, and atomic reopen ACK reconciliation. Generic,
sealed, and legacy services,
scripts, recipes, fixtures, and tests must not invoke one another or mix artifacts/preconditions.

##### Historical combined RED command reference

Run:

~~~bash
uv run --project backend pytest \
  backend/tests/unit/application/test_calendar_aad_digests.py \
  backend/tests/unit/application/test_oauth_refresh_identity.py \
  backend/tests/unit/application/test_oauth_refresh_coordinator.py \
  backend/tests/unit/application/test_connection_capability_use_cases.py \
  -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/m2/test_connection_capability_repository.py \
  backend/tests/integration/m2/test_credential_rotation_repository.py \
  backend/tests/integration/m2/test_oauth_refresh_coordinator.py \
  backend/tests/integration/api/test_connections.py \
  backend/tests/integration/google/test_oauth_flow.py \
  backend/tests/integration/google/test_gmail_sync.py \
  backend/tests/integration/google/test_calendar_sync.py \
  backend/tests/integration/microsoft/test_oauth_flow.py \
  backend/tests/integration/microsoft/test_mail_sync.py \
  backend/tests/integration/microsoft/test_calendar_sync.py \
  backend/tests/integration/operations/test_calendar_aad_0019_preflight.py \
  backend/tests/integration/operations/test_calendar_aad_0019_recovery.py \
  backend/tests/integration/operations/test_postgres_backup_restore.py \
  backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py \
  -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/retention/test_m2_action_retention.py \
  backend/tests/integration/retention/test_role_permissions.py \
  backend/tests/unit/test_init_db_roles_script.py \
  -q
bash scripts/test-legacy-backup-conversion.sh
bash scripts/test-tooling.sh
bash scripts/test-deployment.sh
~~~

Expected: FAIL because the versioned digest helpers and independent fixed vectors, fixed-root-key HKDF/HMAC identity,
two-channel coordinator state machine and zero-call unknown/retry contracts, commit-ACK-loss versioned result-union
reconciliation versus definite rollback, closure/current-readiness separation, automatic started/confirmed handling,
strict disposition/`refresh_identity_changed`/identity-equality parsing, explicit progressive recovery
started/capability-converging unsatisfied/replacement plus atomic rollback boundary,
classified sanitized Google/Microsoft error-callback parity,
targetless pre-save block,
shared full-row/generation/identity credential snapshot CAS, 0018 revision-global advisory lease,
existing-AuditEvent durable refresh fence, atomic confirmed event, fence-aware retention,
active-refresh/typed-rollout preflight, ordinal-aware marker-only task planner, dedicated task kind,
backup management/global/local/schema locks and revision CAS, immutable image entrypoint ownership, reset lifecycle
guard, three-state catalog facts, state-v4 psql-backend registration and deferred-guard pg_restore stream/ordinal
reconcile/completed-call+completion-GUC atomic reopen, registry-scavenged legacy conversion lifecycle, owner-before-secret
artifact/image/script guard, shared-primitive exact-image sealed restore, holder-session operation-specific verifier,
marker-aware exact-scope path, one-off CLIs, and
stopped-service/rollout-guard recipes do not exist.

##### Detailed 27A–27C refresh, preflight, and exact-scope implementation protocol

Create the typed preflight use case, SQLAlchemy repository, and CLI; the CLI and adapter resolver must not contain
ad hoc SQL. The repository requires revision 0018, references only columns present in 0018, rejects partial description/location triples, derives the
distinct affected `(user_id, connection_id, calendar_id)` set only from complete historical triples, and returns
minimal frozen projections. Before any provider call it proves the exact non-directory cursor, owning connection
in `connected`, enabled `calendar.read`, both access and refresh credential rows, and exact ProviderCalendar with
matching user/connection ownership. Access-only is invalid. The projection includes only the encrypted refresh
credential, persisted canonical connection scopes, provider, timezone, opaque pair identity, current
`authorization_generation`, and a frozen snapshot for both credential rows. Each snapshot has exact `id`,
`user_id`, `connection_id`, `credential_kind`,
`ciphertext`, `nonce`, `key_version`, `token_expires_at`, and `updated_at`; it never returns token plaintext or
provider content. It must not insert, update, or delete CalendarEvent, SyncCursor, marker, task, approval, or
execution facts. Refresh fencing reuses the append-only `AuditEventModel` already available in revision 0018;
do not add a migration, table, OAuth-only service, or Task 18 command for this path.

Implement `calendar_aad_digests.py` as the only canonical encoder used by preflight, fence lookup, retention,
rollout guards, tests, and evidence. Every field uses `NULL => 0x00` or non-NULL
`=> 0x01 || uint32_be(length) || raw_bytes`; empty bytes remain distinct from NULL. UUIDs are lowercase canonical
ASCII, integers are unsigned canonical decimal ASCII without `+` or redundant leading zeros, UTC timestamps are
RFC3339 with exactly six microseconds and `Z`, and ciphertext/nonce remain raw bytes.
`credential_snapshot_digest_v1` is lowercase SHA-256 over domain
`b"AIEMPLOYEE/calendar-aad/credential-snapshot/v1\x00"`, followed by the access row and refresh row;
`authorization_generation` is a separate fence/consumption field and is not serialized into this digest. Each row is
ordered exactly as `credential_kind`, row `id`, `user_id`, `connection_id`, `ciphertext`, `nonce`, `key_version`,
`token_expires_at`, `updated_at`. `refresh_credential_snapshot_digest_v1` uses the same framing and row order over
domain `b"AIEMPLOYEE/calendar-aad/refresh-credential-snapshot/v1\x00"`, followed only by the refresh row.
`rollout_digest_v1` uses domain
`b"AIEMPLOYEE/calendar-aad/rollout/v1\x00"` and exact fields
`calendar_aad_0019_preflight.v1`, `20260809_0018`, `20260809_0019`, validated basename, exact lowercase
`sha256:...` image content ID, and integer `900`, in that order. None of the protocols includes token plaintext, raw
scope, provider response, or raw `calendar_id`; any field/encoding/order/domain change requires a new version.
The two credential digests are physical snapshot identities only; ciphertext, nonce, key version, `updated_at`, or
either digest changing cannot prove refresh plaintext replacement. The unit test uses the complete independent
base64 vectors in design section 17.3, verifies exact lengths `497`, `265`, and `222`, and fixes digests
`607c4c12cb3e4ff7736cb56f06b9dd2f63d2681c8d7a36c7801160eaafcaa84a`,
`3a4b1dbd0e77c915067c3af352772580949957815573c953baa41131827582f0`, and
`0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5`, never the production helper for expected
values.

Implement `oauth_refresh_identity.py` as the only canonical `refresh_token_identity_v1` helper. API deps, mail
Worker, calendar Worker and the preflight CLI composition root all read the same `APP_MASTER_KEY_FILE` bytes and use
the same fixed `AeadCipher.key_version` to construct both AEAD and identity services. Derive the fingerprint key with
the fixed HKDF-SHA256 salt/info labels in design section 17.3, then HMAC-SHA256 the domain-separated framed non-empty
plaintext; return lowercase hex plus the fixed key version only to controlled application memory/closed fence
metadata. Do not add an independent identity Secret, multi-key lookup, or mutable-root setting. The
independent unit test must hard-code the
synthetic master-key vector (identity key version `7`), derived fingerprint key
`47119abdcf6b97afcdd6416bf5a8b7771b314a630c53c1553aa1789cc02f95c1`, 73-byte message base64
`QUlFTVBMT1lFRS9vYXV0aC9yZWZyZXNoLXRva2VuLWlkZW50aXR5L3YxAAEAAAAZc3ludGhldGljLXJlZnJlc2gtdG9rZW4tQQ==`, and
expected identity `9de29ff352c78092c4b7dae5e0e3093fee49815a3473d75c3876e3b316222564`; expected key/message/HMAC must
come from an independent standard-library path, never the production helper. M2 fixes the APP root key/version;
missing or changed key material fails closed and requires a future ADR/里程碑 rather than an implicit keyring.
Same-plaintext re-encryption must preserve identity and be represented as changed=false; it cannot fabricate rotation
and is not rollback even when physical digests differ. Only a changed=true A→B proof followed by current identity A is
an A→B→A rollback. No test/log/release output may print plaintext, root key, derived key, or real identity.

Implement `application/ports/oauth_refresh.py` and `infrastructure/db/repositories/oauth_refresh_coordinator.py` as
the automatic coordinator used by `calendar_aad_preflight.py`, `sync_mail.py`, `sync_calendar.py`, and both provider
families, plus the explicit-recovery connection lease used by `connections.py`. Automatic source union is exactly
`calendar_aad_preflight | provider_refresh`. An automatic claim freezes generation, complete snapshots, identity and
fixed key version, rejects any unresolved predecessor, commits `oauth.refresh_started` before network, releases
business transactions during I/O, and requires the same lease plus complete snapshot/generation CAS for atomic
`oauth.refresh_confirmed`. A started attempt, response-after-lock-loss, network unknown, CAS miss, or definite
rollback becomes stable non-retryable `oauth_refresh_result_unknown`/`needs_attention`; Taskiq and
`TransientProviderError` re-entry reads the fence and performs zero provider calls. If commit ACK is lost, the
coordinator must use a new database session for read-only user/connection/attempt reconciliation returning
`OAuthRefreshResultV1 = ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1`. Automatic started accepts
confirmed/replacement; recovery started accepts unsatisfied/replacement. A confirmed member is schema-valid only when
missing/same means old == new plus `refresh_identity_changed=false`, or different means old != new plus changed=true;
replacement requires old != new plus changed=true. Any mismatch is rejected. A schema/attempt/source/time-valid
append-only member permanently closes the attempt or attempts allowed by that mapping without comparing current
mutable facts; replacement closes the matching recovery attempt and original automatic fence, while unsatisfied closes
only the recovery attempt. Actual rollback has no member and remains unresolved. Reconcile never backfills a guessed
result, adds a provider/code call, or requires the lost lease/session. It then separately validates current readiness:
exact post resumes directly and later legal refresh/reauth uses current facts. changed=false after access-only refresh
or same-plaintext re-encryption is normal even if the full physical snapshot changed. changed=true followed by current
identity equal to old is A→B→A; that rollback, missing rows, invalid AEAD/ownership, or inconsistent state yields
`oauth_credential_state_conflict` without reopening the attempt. Explicit progressive recovery
uses the same connection lease around its one-time code exchange but never creates another automatic started.
Integration tests run two Workers against one connection and prove exactly one provider call, plus response-after-
lock-loss/CAS-miss cases with no replay. New connection bootstrap is the only no-old-credential exception; existing
connections never use `ensure_connection` or an unconditional upsert to bypass the protocol.

Extend the existing Google/Microsoft progressive-authorization start/callback use case rather than introducing an
OAuth-only service. The original automatic `oauth.refresh_started` records `fence_generation=F`. When the original
connection is still `connected`, the progressive reauthorization start
locks it, reads `source_generation=S`, the current refresh snapshot, and exactly one matching unresolved fence, and
requires `S >= F` plus current identity recomputed with the fixed key version equal to the fence old identity;
physical digest is only an old-snapshot CAS input. Only then may the existing
`set_capabilities_authorizing` perform its existing single increment to `target_generation=T=S+1`; the existing
`OAuthAttempt.target_authorization_generation` persists only `T`. In that same transaction create the OAuthAttempt
and append `oauth.refresh_recovery_authorization_started` with exactly the recovery schema/source, OAuthAttempt ID,
connection digest, original refresh attempt/started source, F/S/T, both frozen pre-digests, old identity/fixed key
version and stable result code. Its `created_at` must be strictly later than the original automatic started. Do not
add a second persisted generation field or a 0018 migration. Other generation changes between `F` and `S` never
release the fence by themselves.

Keep provider token exchange outside the database transaction. During callback persistence, lock the target
connection and enforce the existing anti-replay equality
`connection.authorization_generation == OAuthAttempt.target_authorization_generation == T`; derive `S=T-1`, load
the old refresh credential and the unique matching unresolved fence under that target-`T` lock, and recheck
`F <= S` plus the exact old snapshot/identity. A stale callback or concurrent later reauthorization fails closed on
the target mismatch. Resolve the recovery association by `OAuthAttempt.id`, obtain the same connection lease, and
consume state/code exactly once before exchange. Decrypt old refresh plaintext only in controlled memory and compare
canonical bytes with `secrets.compare_digest`. A provider response is eligible to consume the original fence only
when it explicitly contains a non-empty different refresh token and normalized identity/scope/expiry validation
passes. One short transaction then CASes target `T`, writes access/refresh credentials, actual scopes and requested
capability states, and appends `oauth.refresh_credential_replaced`; generation remains `T` and never increments
again.

The replacement event has non-null `user_id` and closed metadata containing exactly
`proof_schema_version="oauth_refresh_credential_replaced.v1"`, `source="progressive_recovery"`, original
`started_source`, `connection_digest`, original `refresh_attempt_id`, `recovery_oauth_attempt_id`,
`recovery_authorization_started_event_id`, `fence_generation=F`, `source_generation=S`,
`target_generation=T`, `pre_generation=post_generation=T`, both original pre-digests, both required post-digests,
old/new `refresh_token_identity_v1` with fixed key versions, `refresh_identity_changed=true`, actual persisted expiry,
and stable result code. Enforce old != new,
`F <= S`, `T=S+1`, exact old snapshot/fence and generation CAS, and `created_at` strictly later than both original
automatic started and matching recovery-started event. It never persists token plaintext, a plaintext hash, raw
scope, or provider response. Failure injection after any credential, scope, capability, or event mutation and during
commit must roll back the full callback transaction; a network response alone is never evidence. A valid replacement
event permanently closes both the matching recovery started and original automatic fence.

Missing/empty/same refresh, user denial, missing administrator consent, safely known provider/network failure, or
identity mismatch does not save credential/scope/account facts and does not consume the original fence. The start
transaction already changed this attempt's requested capabilities to `authorizing` at target `T`. Extend the existing
`mark_progressive_authorization_failed` contract so, while the connection is still `connected` at generation `T`, one
short transaction changes only those requested capabilities to `action_required`, writes the stable error code,
preserves their existing `actual_scopes` and last-verified facts, and appends
`oauth.refresh_recovery_unsatisfied`. Its closed metadata uses
`result_schema_version="oauth_refresh_recovery_unsatisfied.v1"` and binds the matching
recovery-started event/OAuthAttempt, original fence, F/S/T, both frozen pre-digests, old identity/fixed key version,
canonical requested capabilities, `capability_transition="action_required"`, stable error/result codes, and a
`created_at` strictly later than recovery started. It contains no credential post snapshot/digest, new identity,
persisted expiry, or deadline field. If target `T` is stale, the capability update is an idempotent
no-op and an otherwise-safe audit may use `capability_transition="stale_target_noop"`; it must not overwrite newer
authorization state. Unsatisfied closes only that OAuthAttempt and leaves the original automatic fence unresolved;
replacement closes the OAuthAttempt and consumes the original fence.
The user may explicitly create another progressive OAuthAttempt, but neither the prior state/code nor an exchange with
unknown result may be replayed. Lease loss, CAS miss, definite rollback and commit-result-unknown use the coordinator
reconciliation contract rather than fabricating an unsatisfied or replacement result.
Disconnect deletes the old credential, so reconnecting or mapping a new connection cannot reconstruct or fabricate
replacement proof. If the affected scope remains, only an existing explicitly user-authorized data-disposition flow
may remove it; otherwise stop and request a newly approved design.

Keep the ordinary targetless OAuth path only for bootstrap or unfenced identity merge. If its normalized provider
identity resolves to an existing connected row with an unresolved automatic fence, return
`oauth_refresh_recovery_requires_connection_start` before saving any credential, scope, or capability. It may append
only an optional content-free blocked audit. Do not add candidate snapshot fields, a migration, targetless
consumption, a second connection, or an unconditional upsert; the user must begin a new connection-bound progressive
OAuthAttempt instead.

Update `api/routers/connections.py` so Google and Microsoft callbacks share one closed response-shape discipline:
require non-empty `state` and accept exactly one of `code` or bounded non-empty `error`. Missing state, both values,
neither value, or malformed error rejects before state consumption. Every valid error—including an unknown provider
name—must invoke `ConnectionsUseCase.callback_error(...)`, consume state once, and for target-bound recovery persist
the matching unsatisfied result/transition. The common contract is state consumption, target-`T` convergence,
redaction, and replay rejection; preserve the safe classification matrix for the Problem and persistent stable error
code. User denial and unknown names use `oauth_authorization_failed`; Microsoft consent evidence uses the existing
`microsoft_admin_consent_required` plus administrator guidance; ordinary Microsoft `interaction_required` keeps
`microsoft_reauthorization_required`. Raw error/description/codes are never stored, logged, traced, or reused as an
error code. A later same-state callback using the same or another unknown error is rejected as replay.
Neither provider error branch may reach credential persistence or weaken targetless fenced-identity blocking.

Before the CLI reads/checks a rollout artifact or invokes any provider, create a dedicated database connection and
execute `SELECT pg_try_advisory_lock(20260809, 19)`. These fixed session-lock keys are the complete global domain
for revision 0019; basename, user, connection, and calendar never affect them. A false result raises
`calendar_aad_rollout_locked` with zero provider call, zero persistent write, and zero artifact mutation. Keep the
same connection open through final artifact publication, separate from all short application transactions. Before
each provider call, credential CAS commit, and artifact publish, use that same session to verify liveness and actual
ownership of the lock; do not reacquire reentrantly. Connection loss or missing lock fails closed at the next
boundary. Process/connection exit supplies the correctness release through PostgreSQL session cleanup; an
in-process mutex or basename lock is insufficient.

The revision-global advisory lease is not the unknown-result protection. For each connection, first obtain the shared
coordinator's connection-scoped session lease, freeze the current generation and both credential rows, derive
`credential_snapshot_digest_v1` from the rows and `refresh_credential_snapshot_digest_v1` from the refresh row, and
decrypt the refresh token under its exact credential AAD in controlled memory. Validate non-empty canonical UTF-8 and
the existing token boundaries, then compute current `refresh_token_identity_v1` with the fixed APP root key/version.
Inspect
existing content-free fence events before provider access. An `oauth.refresh_started` without matching versioned
confirmed or valid explicit progressive replacement consumption blocks provider access for every
basename. Confirmed lookup requires the exact canonical attempt/source, revision/rollout tuple, G/G/G, both
pre-digests, both required post-digests, old/new identity/version, persisted expiry, disposition,
`refresh_identity_changed`, deadline candidate and strict timestamp. Missing/same must be old == new/changed=false;
different must be old != new/changed=true, otherwise the event does not close the attempt. If current credentials match historical
post, the rollout resumes from persisted token/expiry; if a later valid refresh/reauth changed them, current readiness
uses the current token/expiry, both without another old-attempt call. Only `oauth.refresh_credential_replaced` from explicit progressive recovery can
close an older fence, and only when original attempt/started source/F, both original pre-digests, old
identity/version, recovery OAuthAttempt/event IDs, F/S/T, both post-digests, different-token new identity/version,
`refresh_identity_changed=true`, persisted expiry, old != new and strict timestamps are valid; it closes recovery and
original automatic attempts. Later
generation/capability/scope/access/refresh changes do not reactivate a validly closed attempt. Preflight independently
validates current readiness and freezes current generation/snapshots for any new automatic attempt. Current identity
equal to old == new after a changed=false result, access-only refresh, or same-plaintext re-encryption is normal even
when physical digest differs. Current identity equal to old after changed=true is A→B→A; that rollback, missing rows,
invalid AEAD/ownership, or inconsistent current facts yields `oauth_credential_state_conflict`, blocks rollout, and
never recalls provider.
Basename changes, automatic different-token refresh, access-only rotation,
generation/physical-digest changes, same-plaintext re-encryption, direct SQL, or an operator waiver cannot reset the
fence.

Implement `application/ports/oauth_refresh.py` plus `application/ports/credential_rotation.py` and their
session-bound repositories as the only existing-connection credential writer boundary. Automatic requests carry
source, generation, complete access/refresh snapshots and old identity/fixed key version; the implementation locks
connection → access row → refresh row → matching started audit, rechecks every field, and atomically appends
`oauth.refresh_confirmed`. Explicit progressive recovery uses the same snapshot/CAS helper inside the callback
transaction so credentials/scopes/capabilities and either replacement proof or unsatisfied result remain atomic. The
read-only side exposes only the versioned discriminated result union, followed by a separate current-readiness check;
`sync_mail.py`, `sync_calendar.py`, mail/calendar Workers and preflight call the automatic ports directly;
`connections.py` calls the explicit-recovery lease/result path. Remove unconditional mutation semantics from
`email.py` instead of leaving a second writer. Unknown, lock-loss, CAS-miss and database-confirmed rollback paths leave
the automatic fence unresolved; duplicate delivery reads it and makes zero provider calls. A lost commit ACK must
enter the separate fresh-session read-only result reconcile described below.

Compare old/new refresh plaintext in controlled memory before persistence. A provider response with no refresh token
or the same plaintext CAS-updates access only and preserves every refresh-row byte, with old == new and
`refresh_identity_changed=false`; a different non-empty token updates both rows with old != new and changed=true.
Every known-valid automatic response—missing, same, or different—appends matching `oauth.refresh_confirmed` with
`refresh_token_disposition` and `refresh_identity_changed`; none appends replacement consumption. An unresolved
predecessor prevents automatic provider access, so a Worker can never repair it by rotation. Multiple unresolved
fences, stale generation/identity, any row CAS miss, lock loss, unknown result, or definite rollback changes nothing.
Lock and recheck the started row immediately before confirmed append so mail/calendar duplicate deliveries produce
exactly one provider call/result; Taskiq/`TransientProviderError` re-entry after started is a zero-call lookup.

After all local credential existence/ownership/decryption checks succeed and immediately before the first permitted
provider call, verify both the revision-global lease and the connection coordinator lease, then use a short
application transaction to append
`oauth.refresh_started` with `task_id=NULL` and `actor_type=system`, and commit it before entering the
adapter. Generate one random UUID for the newly permitted attempt, serialize it as canonical lowercase text, and
reuse it for every result event. Metadata is a closed content-free schema containing only
`fence_schema_version="oauth_refresh_fence.v1"`, `source="calendar_aad_preflight"`,
`refresh_attempt_id`, source/target revision, `rollout_digest_v1`, `connection_digest`, `fence_generation=F`,
`pre_credential_snapshot_digest_v1`, `pre_refresh_credential_snapshot_digest_v1`, old
`refresh_token_identity_v1`, the fixed `refresh_token_identity_key_version`, and `result_code`. It must
not contain token bytes, raw scopes, provider response, body, raw `calendar_id`, or credential timestamps beyond
the explicitly approved expiry on a confirmed result. Network I/O never occurs inside this transaction.

`calendar_aad_preflight_0019.py` accepts no scope arguments, resolves the same credential-aware Google/Microsoft
CalendarReader construction used by the ordinary Calendar Worker, but first groups affected pairs by connection
UUID and processes those groups in deterministic serial order. For each distinct connection with a newly committed
started fence, use the refresh token already decrypted under the exact credential AAD and proactively call the
existing provider-neutral OAuth adapter's `refresh()` exactly once before any `initial_pages`; a recovered
confirmed fence calls it zero times. Never use the old access token as a migration substitute and never wait for a
resource 401.
Validate the returned `OAuthTokenSet` plus provider adapter invariants: non-empty
bounded access token, positive bounded `expires_in`, optional valid rotated refresh token, and
`granted_scopes` covering the connection's persisted canonical scopes as well as the adapter's
`calendar.read` requirement. Network unknown result, `invalid_grant`, reduced/unknown scope, malformed token/
expiry/scope, or an expiry at or before `now + 900 seconds` fails before any pair probe and persists no capability/
connection status change. Append only the stable content-free needs-attention disposition allowed by the fence
protocol; never treat a provider error as permission to replay the same snapshot.

After the network call and validation, call the shared credential-rotation repository; do not call an unconditional
OAuth save/upsert or retain Calendar Worker `rotate_access_token()` as another boundary. In one short transaction,
recheck the owning connection, exact `authorization_generation`, and enabled `calendar.read`; conditionally match
both access and refresh rows against every frozen snapshot column; update access-token AEAD and exact
`token_expires_at`; update refresh-token AEAD only for a different returned plaintext, otherwise preserve the proven
old row byte-for-byte; and append `oauth.refresh_confirmed` with
`result_schema_version="oauth_refresh_confirmed.v1"`, the same source/started source and attempt UUID,
source/target revision, `rollout_digest_v1`, hashed connection,
`fence_generation=source_generation=pre_generation=post_generation=F`, both pre-digests, both required
post-digests, old/new identity with fixed key versions, actual persisted `token_expires_at`,
`refresh_token_disposition="missing"|"same"|"different"`, `refresh_identity_changed`, required
`rollout_deadline_candidate = token_expires_at - 900 seconds`, and a stable result code. Its `created_at` is strictly
later than started. All three known-valid dispositions use this path; different updates the refresh row but does not
append `oauth.refresh_credential_replaced`. Enforce missing/same ⇒ old == new and changed=false; different ⇒ old !=
new and changed=true. Reject inconsistent metadata in the shared result parser.
Credential mutation and confirmed event are one atomic commit.
Both row-count/snapshot predicates must succeed or the transaction rolls back and raises a stable needs-attention
credential-conflict result. CAS miss, lease loss after provider response, or a database-confirmed rollback never
overwrites a newer token, never runs pair probes, never publishes the artifact, leaves revision 0018, and leaves the
started fence unresolved so all later basenames make zero provider calls until exact explicit progressive replacement
proof exists and current preconditions independently pass. If commit was issued but its ACK is lost or an exception
cannot prove rollback, never call the provider again and never reuse the lost session/lease as evidence. Open a new
database session and read only by `user_id + connection_id + attempt_id`, returning the versioned
confirmed/unsatisfied/replacement result union. A legal matching member permanently closes only its allowed started;
replacement also consumes original automatic, unsatisfied does not. The parser enforces the confirmed
disposition/flag/equality matrix and replacement old != new/changed=true before closure. Missing or invalid proof leaves that started
unresolved and enters `needs_attention`; do not backfill or guess a result. The closure query does not compare current
mutable rows. A second readiness step accepts exact post-state, accepts a later legitimate refresh/reauth by using
current facts, accepts changed=false after access-only refresh or same-plaintext re-encryption, or returns
`oauth_credential_state_conflict` for changed=true A→B→A old identity, missing rows, invalid AEAD/ownership, or
otherwise inconsistent current state. Every branch keeps subsequent `provider_calls == 0` for the closed/unknown
attempt. Invalid/unknown automatic outcomes do not create a separate terminal event. Fence lookup, crash recovery,
unknown-result blocking, explicit recovery and retention share the same versioned metadata matching; current readiness
does not require full credential lineage. Provider calls remain outside database transactions.

If the atomic credential/confirmed transaction commits and the process crashes before artifact publication, a
later invocation must recognize the closing result. Exact current post-state reuses that persisted token/expiry;
another legitimate refresh/reauth uses current token/expiry after readiness, without replaying the closed attempt.
After every distinct connection has a closed result and current-ready token, verify the applicable persisted/current
access-credential expiry by reloading the current rows; do not read the historical confirmed deadline candidate as a
deadline input. Compute with an injected UTC clock and fixed constant
`CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS = 900`:

~~~python
current_deadline = min(current_access_token_expires_at) - timedelta(seconds=900)
effective_deadline = min(original_artifact_deadline, current_deadline)
~~~

For the first successful artifact, `original_artifact_deadline = current_deadline`; later invocations preserve that
original value. A shorter later refresh/reauth expiry immediately tightens the effective deadline, while a longer
expiry never extends it. The confirmed `rollout_deadline_candidate` is retained only for historical result-schema
validation. Do not derive the margin from mutable settings or accept an operator-provided timestamp. If
`now >= effective_deadline`, fail before probes. For zero affected pairs, write an explicit no-op state with zero
counts and no deadline; only an exact empty-set recheck may use that branch. Model rollout state as a closed union: a
non-empty state requires positive count-matched digest arrays plus non-null original/effective deadline facts and
applies `now < effective_deadline`; a zero state requires both counts and arrays empty plus exact null timestamps.
Every backup/audit/migration/recovery guard reloads current access expiry, re-derives the affected set, and applies
the same union, so `null` is never compared as a timestamp and never acts as a blanket guard bypass.

Then call only `initial_pages(scope_key)` for each deterministic pair with the newly refreshed access token.
Consume the complete bounded page stream in controlled memory, reject a missing final cursor or scope-mismatched
event, and discard all provider content after validation. Do not call `directory_pages()`, `sync_pages()`, a
connection-level owner, another pair, or any provider write adapter. A resource 401 after proactive refresh fails
immediately and must not call refresh again. Reauthorization, provider revocation, 403, malformed pages, timeout,
deadline crossing, or any pair failure makes the command nonzero without connection/capability status mutation.
For a non-empty state the CLI reloads current access expiry and checks `now < effective_deadline` before each probe and immediately before artifact
publication; for the zero branch it instead repeats the exact empty-set check. It then atomically publishes the
content-free state artifact at
`${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json` with mode `0600`. The artifact contains
only schema/revision, `rollout_digest_v1`, safe basename, immutable image content ID, earliest expiry/deadline, fixed margin,
affected/connection counts, hashed connection IDs, pair digests, and stable result codes; it stores no token,
scope string, provider response, or raw calendar ID.
The CLI wraps each refresh/probe step in `settings.task_step_timeout_seconds`, the whole invocation in
`settings.task_timeout_seconds`, and treats either timeout as failure; those budgets do not change the fixed
900-second rollout margin.

The canonical JSON artifact has exactly these keys and no extras:

~~~json
{
  "schema_version": "calendar_aad_0019_preflight.v1",
  "source_revision": "20260809_0018",
  "target_revision": "20260809_0019",
  "rollout_digest_v1": "0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5",
  "backup_artifact_basename": "calendar-aad-0019-synthetic",
  "immutable_image_id": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "affected_connection_count": 1,
  "affected_pair_count": 2,
  "connection_digests": ["lowercase-sha256"],
  "pair_digests": ["lowercase-sha256"],
  "earliest_token_expires_at": "2030-01-01T01:00:00Z",
  "rollout_deadline": "2030-01-01T00:45:00Z",
  "safety_margin_seconds": 900,
  "result_code": "calendar_aad_preflight_passed"
}
~~~

`connection_digest` is lowercase SHA-256 of `20260809_0019 + NUL + connection_id`; pair digest keeps the existing
revision/connection/calendar formula. Arrays are sorted, unique, and count-matched. The zero-pair branch uses empty
arrays, zero counts, exact `null` for both timestamps, and `result_code="calendar_aad_preflight_zero"`; no other
null timestamp is valid. Recompute `rollout_digest_v1` from the canonical v1 subset and reject any stored value,
basename, revision, image ID, or margin mismatch before DB/provider access. Serialize canonically to a
same-directory `0600` temporary file, fsync it, recheck the
typed rollout guard, affected set, basename, and image binding, then rename atomically; a failed run must leave no
success artifact.

The host recipe resolves the actual local content ID of the Compose-selected backend image before starting any
rollout one-off and passes it as internal `CALENDAR_AAD_IMMUTABLE_IMAGE_ID`; the CLI validates its shape and stores
it as `immutable_image_id`. The operator supplies only the already-required immutable `APP_IMAGE_TAG`, never a
separate content ID. Backup, every audit phase, migration, resync, and restored-0018 verification repeat the same
host resolution and compare the resulting content ID with the artifact, so a moved tag or accidentally selected
image fails before database access or artifact publication.

Define one pure application-layer pair-digest helper with the fixed target revision and NUL-separated canonical
input. Preflight output, recovery planning, Worker verification, tests, and release evidence must reuse this helper
or its exact test vector; duplicating subtly different digest formulas is forbidden.

Create a typed recovery use case and SQLAlchemy repository; the CLI and Worker must not contain ad hoc SQL.
The repository checks revision 0019, reads only rows matching all of:

~~~sql
sync_cursors.resource_kind = 'calendar'
AND sync_cursors.scope_key <> 'directory'
AND sync_cursors.last_error_code = 'calendar_event_resync_required'
~~~

Join each cursor to its owning OAuth connection and current ProviderCalendar; never accept an operator-supplied
user, connection, or calendar filter. Order pairs deterministically by connection UUID and calendar ID. Lock each
exact cursor row with `FOR UPDATE` or equivalent compare-and-swap before inspecting or allocating attempts. Compute
`pair_digest` as lowercase SHA-256 of
`20260809_0019 + NUL + connection_id + NUL + calendar_id`; never place the raw calendar ID in an idempotency key.

For that exact kind/revision/pair, parse and validate every existing recovery ordinal. Treat `created`, `queued`,
`running`, and `retry_scheduled` as active: exactly one is always returned/reused without allocating any new
ordinal, and more than one fails closed. If no prior attempt exists, allocate ordinal 1. A later ordinal is allowed
only when no active attempt exists, the highest ordinal is `failed` or `cancelled`, and the marker remains; then
allocate `max_ordinal + 1`. No other prior state permits a new ordinal. `succeeded` with a surviving marker, or
`waiting_approval`, `reconciling`, or `needs_attention`, is an invariant failure. Never update or resurrect an old
terminal TaskRun. The new user-scoped idempotency key is
`calendar-aad-0019:<pair_digest>:attempt:<ordinal>`, and task input has exactly
`connection_id`, `scope_key`, `recovery_revision`, `pair_digest`, and `recovery_attempt_ordinal`.

In the same application-owned transaction, create the new ordinal's `calendar.aad_0019.resync` TaskRun,
content-free audit, and initial Outbox. A uniqueness winner for the same ordinal returns the existing task only
after verifying kind, exact payload, digest, ordinal, and key; any mismatch fails closed. Reusing an active attempt
does not create another audit or Outbox. A single planner invocation processes each pair once and returns exactly
the active ordinal or at most one newly created ordinal for it. The planner validates the bound rollout artifact
and typed rollout guard before locking/creating tasks and immediately before the transaction commit; expiry or
zero-state drift creates no TaskRun/Audit/Outbox and never advances an ordinal.

After commit, `calendar_aad_0019.py` must use the existing exact-task Outbox dispatch path with an in-process
recovery-only queue port, then call `DurableTaskRunner` synchronously for only those returned task IDs. It must
not publish to Redis, call an unfiltered `relay_once`, consume an existing queue, or start Taskiq. A crash before
or after exact dispatch remains recoverable from PostgreSQL/Outbox; a later Outbox replay of a terminal task is
a provider-call-free no-op. The CLI must not call the planner again after an attempt it just executed fails, so
ordinal+1 requires a later explicit operator invocation. Existing runner `started_at` total timeout, step timeout,
`max_transient_retries`, and persistent `attempt_count` bound retries inside one TaskRun and are independent of
the recovery ordinal. The CLI and recovery Worker both validate the same rollout artifact and typed rollout guard
at start, before provider access, and immediately before the final marker-aware local commit; expiry or zero-state
drift preserves the marker and returns nonzero. The CLI returns nonzero when any marker remains failed/unresolved
and prints only counts, pair digests, ordinals, earliest deadline, and stable content-free error codes.

For `calendar.aad_0019.resync`, require the five exact input keys, a canonical UUID, non-empty/non-padded scope
within the existing 512-character boundary, `scope_key != "directory"`, fixed recovery revision, lowercase
64-character digest, and a positive integer ordinal before resolving credentials or adapters. Recompute the
digest and expected `calendar-aad-0019:<pair_digest>:attempt:<ordinal>` key, then recheck TaskRun `user_id`
ownership, connected/enabled `calendar.read`, both access/refresh AEAD credentials, the exact ProviderCalendar row, and an existing cursor whose
cursor/freshness are still invalidated and whose error is the marker. The recovery state lookup must never
synthesize a missing cursor or reinterpret TaskRun `attempt_count` as the recovery ordinal.

Add a public `SyncCalendarUseCase.execute_marked_scope()` that reuses the ordinary bounded event-page and v2
encryption path but cannot call `_execute_directory()`. Its final short transaction must recheck the same marker
as a CAS precondition before any event upsert becomes visible, then atomically write events, exact cursor,
freshness, content-free audit, and marker clearance. Marker absence before the provider call is a no-op; marker
loss or ownership/capability change after the read rolls back the local write. A provider failure preserves the
marker. Do not add a directory fallback, full-account sync, direct provider write, ApprovalRequest,
ToolExecution, Task 18 command, or legacy v1 decrypt path.

##### Detailed 27D–27E runbook, audit, restore, and deployment protocol

Document Google/Microsoft progressive recovery and targetless fenced-identity pre-save blocking, the shared
`OAuthRefreshCoordinator` two-channel protocol, strict `refresh_identity_changed` result parsing, fixed APP
root-key/version, changed=false same-plaintext re-encryption, changed=true A→B→A rollback, Microsoft
administrator consent, global/provider kill switches, dedicated
test-account allowlist, commit-ACK-loss versioned result-union reconciliation versus definite rollback,
closure/current-readiness separation with `oauth_credential_state_conflict`, classified Google/Microsoft
error-callback parity and raw-error redaction, unsatisfied capability convergence, calendar restore, and duplicate/mis-send incident response.
Once 0019's post-resync audit has passed, any general service has started, or any business write has occurred, define
the application rollback floor as an 0019-compatible image that understands the version columns and writes only v2:
pre-0019/M1, v1 readers, and old Calendar writers are forbidden even when every Task is terminal. Preserve the
stricter task-state rule that an older but still 0019-compatible M2 image may be selected only after all Tasks are
terminal; `reconciling` or `needs_attention` requires a forward-fix image with reconciliation support. The only
pre-0019 exception is the same sealed maintenance window before any general-service restart/business write: if
recovery cannot finish before the current-row `effective_deadline`, restore the whole PostgreSQL database from that window's
refresh-after/pre-migration encrypted backup and verify revision 0018 before starting the recorded original
0018-compatible immutable image. Never use Alembic downgrade for this exception.

The runbook and acceptance checklist must define the non-rolling production sequence exactly: keep write switches off; record the original 0018-compatible immutable image; disable Calendar scheduling and stop new ingress; gracefully drain and stop every pre-0019 CalendarEvent reader/writer—Caddy, API, Worker, and Scheduler—while PostgreSQL and Redis remain running; confirm no old `sync_calendar`, event read, or upsert remains; run only `just calendar-aad-preflight-0019`, which first obtains the fixed revision-global PostgreSQL session lease, then obtains the shared coordinator lease for each distinct affected connection, commits started before provider access, and commits credentials only through complete old-snapshot/generation/identity CAS before creating the bound original-deadline or exact zero/no-deadline artifact; only after every pair passes and the typed rollout guard succeeds, back up and run the pre-migration audit; use only `just calendar-aad-migrate-0019`; run the post-migration audit; make the v2-only immutable image available while all four general services remain stopped; run only `just calendar-aad-resync-0019`; at every boundary reload current access expiry and use the non-extending effective deadline; run and commit the post-resync audit before that effective deadline or under the still-empty zero branch; only after it passes start API/Worker/Scheduler/Caddy; and then require `just health`. Every recipe/CLI checks the artifact, actual image content ID, current access expiry, effective deadline, and typed rollout guard at start and immediately before critical DB/artifact commits. Cursor mutation and all evidence must use the exact `(connection_id, calendar_id)` pair, never `calendar_id` alone. Tasks 27A–27E document and test this contract but do not execute production changes, and Task 16A test success must not be treated as rolling-deployment evidence.

The preflight portion additionally commits content-free append-only `oauth.refresh_started` before each provider
call and atomically commits complete old-snapshot CAS, new credential/expiry, and `oauth.refresh_confirmed`
afterward. Started binds canonical attempt/source, revision/rollout, G, both pre-digests and old identity/fixed key
version. Every known-valid missing/same/different response must be confirmed with G/G/G, both pre/post digests,
old/new identity/version, persisted expiry, disposition, `refresh_identity_changed`, deadline candidate, stable result
code and strict `created_at`; missing/same preserves the refresh row byte-for-byte and requires old == new/changed=false,
while different requires old != new/changed=true and updates it without consuming an
older fence. An unresolved fence blocks every basename and all automatic Worker refresh until explicit progressive
recovery appends `oauth.refresh_credential_replaced` with original attempt/started source/F, both original
pre-digests, old identity/version, recovery OAuthAttempt/event IDs, F/S/T, both post-digests, different-token new
identity/version, `refresh_identity_changed=true`, persisted expiry, old != new and strict timestamps. The consumption permanently closes both matching recovery
and original automatic attempts. Every legal append-only confirmed/unsatisfied/replacement result permanently closes
its allowed attempt through the versioned result union; the shared parser rejects any confirmed
disposition/flag/equality mismatch or replacement without old != new/changed=true, and unsatisfied never consumes
original fence. Closure remains
valid through later generation/capability/scope/credential changes; preflight independently checks current readiness
and recomputes current identity with the fixed APP root key/version. changed=false after access-only refresh or
same-plaintext re-encryption is normal; changed=true followed by current identity equal to old is A→B→A. That rollback,
missing rows, invalid AEAD/ownership, or inconsistent facts is `oauth_credential_state_conflict` without reopening the attempt.
Credential handling follows decrypt → non-empty/UTF-8/bound validation → identity → committed started/lease checks →
provider access. A result commit ACK loss always opens a fresh read-only database session and queries the versioned
confirmed/unsatisfied/replacement union. Legal matching result actual commit permanently closes the corresponding
attempt; actual rollback has no result and remains unresolved. Reconcile adds no provider/code call. It then performs
current readiness separately: exact post resumes from persisted credential/expiry, another legal refresh/reauth uses
current facts, changed=false access-only/same-plaintext re-encryption remains non-conflicting, and changed=true
A→B→A/missing-row/invalid-AEAD-or-ownership conflict blocks with `oauth_credential_state_conflict` at zero calls. Retention
uses the same union, preserves unresolved automatic fences, treats unsatisfied recovery pairs separately, and deletes
automatic-success, unsuccessful-recovery, or successful-recovery groups only when every event is older than cutoff,
without later credential lineage.

If provider preflight fails, do not back up for the rollout, audit as ready, or apply 0019. Missing/undecryptable/
revoked refresh, access-only credentials, returned scope shrink, malformed token/expiry, insufficient 900-second
margin, active-refresh resource 401, revision-global or connection-coordinator lock contention/loss, credential CAS miss, or exact-pair probe
failure are all hard failures. A network unknown result, provider-call-after lock loss, `invalid_grant`, malformed
response, scope shrink, CAS failure, database-confirmed rollback, or started-without-confirmed result must leave a
durable content-free fence. Commit-result-unknown must first perform the fresh-session reconcile above and must never
retry the provider or guess a result. Every later invocation, including one with another basename, and every
Taskiq/`TransientProviderError` delivery for an unresolved attempt must make zero provider calls. Recovery requires a
new user-initiated, connection-bound
progressive OAuthAttempt: start records `oauth.refresh_recovery_authorization_started` in the OAuthAttempt
transaction and sets requested capabilities to `authorizing`; callback exchanges that code once without a second
automatic started. Denial, missing/same or safely known provider/network failure at current target `T` must append
the versioned `oauth.refresh_recovery_unsatisfied` result and atomically set only those requested capabilities to
`action_required` with a stable error while preserving actual scopes/last-verified facts; stale `T` is a capability
no-op plus safe audit. Unsatisfied has no credential post/expiry fields and closes only the recovery attempt. The
original fence remains and the user may create another OAuthAttempt. Only a different non-empty refresh token may
atomically commit credential/scopes/
capabilities plus complete old != new, changed=true F/S/T `source="progressive_recovery"` replacement proof. Targetless callback must fail
before any local credential/scope/capability save, and automatic Worker refresh cannot consume the fence. Do not
trigger an extra provider call or temporary OAuth-only service merely to consume it. A generation/physical-digest/
access-only/automatic-different-token change or operator disposition cannot waive replay safety. If confirmed
committed before a crash, rerun from its closing result; use persisted post token/expiry if still current or a later
valid current token/expiry after readiness, never reauthorizing or refreshing the closed attempt. Keep the database at 0018,
restore the original 0018-compatible services while write switches remain off. An unknown fence requires the
explicit progressive recovery flow above before a new full change window; an
independent local-directory failure instead requires restoration of that exact fact and does not waive any fence. If an
affected scope can only be removed through an existing explicit user-authorized data-disposition workflow,
complete that workflow and recompute the affected set. A waiver, direct SQL, fabricated credential/ProviderCalendar
row, full-account probe, or post-migration marker clear is forbidden.

The runbook must warn operators not to disconnect a connection that has an unknown refresh fence. Disconnect deletes
the old credential before a later explicit recovery callback can compare plaintext, so disconnect/reconnect, a new
connection, or a new-token mapping can never create replacement consumption. If the recovery exchange omits refresh
token, returns the same token, the connection is already disconnected, or old plaintext is unavailable, the 0019
scope stays blocked. When
the affected scope still exists, only an existing explicitly user-authorized data-disposition flow may remove it;
if that flow does not apply, stop and request a newly approved design. Fabricated consumption and marker skip are
forbidden. A targetless callback resolving to a fenced identity must be blocked before persistence and prohibit
`ensure_connection`; an explicit recovery response-after-lock-loss, CAS miss, exchange unknown or same-code replay
must preserve the existing row and enter `needs_attention`. Operators inspect only content-free automatic/recovery
attempt, source, lease, CAS, result-union, current-readiness and capability-transition facts. Google/Microsoft callback
accepts only `code+state` or bounded non-empty `error+state`; shape errors reject before consumption and every valid
error including unknown names consumes state once and performs target-T unsatisfied/no-op. Problem/error code follows
the safe matrix: denial and unknown names use `oauth_authorization_failed`, Microsoft consent evidence uses
`microsoft_admin_consent_required`, and ordinary `interaction_required` uses
`microsoft_reauthorization_required`; raw error is not stored/logged or used as a persistent code, and replay fails
closed. M2 fixes the APP root key/version and has no multi-key lookup; changed=false same-plaintext re-encryption is
not conflict, while changed=true followed by current identity equal to old is A→B→A. That rollback, missing rows,
invalid AEAD/ownership, or inconsistent credential state produces `oauth_credential_state_conflict` without reopening
the attempt. Release evidence
records only fixed synthetic HMAC-vector pass/fail, never real token, identity, root key or derived key material.

Implement `just calendar-aad-preflight-0019` with no pair/user argument. Before starting its one-off container,
the host recipe must inspect Compose state and fail when `caddy`, `api`, `worker`, or `scheduler` is running; it
must also prove the database revision is exactly 0018, validate required `BACKUP_DIR` and safe
`BACKUP_ARTIFACT_BASENAME`, and mount that exact backup directory as `/backups`. It may validate the pathname shape
but must not inspect artifact existence/collision outside the database lease; the CLI obtains
`pg_try_advisory_lock(20260809, 19)` first and only then performs the artifact check. Resolve the selected backend
image reference to its actual local content ID and pass only the
resolved value to the container; missing image, `latest`, inspect failure, or later content-ID mismatch is a hard
failure. Use the selected immutable release image whose preflight
path is explicitly compatible with both 0018 and the later 0019 recovery, plus the `worker` service's
existing Secret/environment mounts with `--no-deps --entrypoint /bin/sh`, but run only the fixed
`ai_employee.cli.calendar_aad_preflight_0019` module. PostgreSQL/Redis remain available, no Taskiq process or
general queue consumer starts, and the command writes no Calendar facts. Any allowed OAuth token rotation happens
before the backup so the backup contains the current credential state. The recipe passes only the validated
basename and fixed `/backups/<basename>.calendar-aad-preflight.json` path; an operator cannot supply a deadline.

After the stopped-service/revision precheck, the preflight container invocation is exactly:

~~~bash
docker compose run --rm --no-deps \
  -e "BACKUP_ARTIFACT_BASENAME=${BACKUP_ARTIFACT_BASENAME}" \
  -e "CALENDAR_AAD_ROLLOUT_STATE=/backups/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json" \
  -e "CALENDAR_AAD_IMMUTABLE_IMAGE_ID=${CALENDAR_AAD_IMMUTABLE_IMAGE_ID}" \
  -v "${BACKUP_DIR}:/backups" --entrypoint /bin/sh worker -ec \
  'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_preflight_0019'
~~~

Implement `just calendar-aad-migrate-0019` with no pair/user/deadline argument. It repeats the stopped-service gate,
requires revision exactly 0018, mounts the same rollout state read-only, and uses the `migration` service's owner
Secret boundary with a fixed `ai_employee.cli.calendar_aad_migrate_0019` command. The wrapper validates artifact
schema/basename/actual-image/affected-set binding and the typed rollout guard, targets exactly revision
`20260809_0019`, and reruns role grants after success. Task 16A's migration module already invokes the frozen guard
before DDL/DML, and Task 16A's `backend/migrations/env.py` already invokes the same guard through
`on_version_apply` after the revision operation/version-table update but before final transaction commit. Task 27C
only constructs the artifact-backed guard and passes it through the frozen Alembic
`Config.attributes["calendar_aad_0019_guard"]` boundary. It must not edit or duplicate either invocation. The
same Task 16A outer online Alembic boundary acquires the management lifecycle lease from `postgres`, then target
admission, then exclusive `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` before the migration transaction and holds all
through commit/rollback;
this dedicated wrapper must inherit that behavior, not add a second lock key or release it around the typed guard.
An ordinary backup holding the shared form may delay this migration, and the migration must not commit until the
backup releases after post-revision CAS/local manifest publication. The
concrete guard reloads current access expiry and validates effective deadline at both phases; raising rolls the whole
0019 transaction back. Ordinary migrations retain Task 16A's existing no-attribute behavior, and fresh/strict-zero
`upgrade head` retains its built-in zero-bootstrap path. The wrapper never accepts `head`, a raw revision, an
operator timestamp, or an allow/waiver flag.

After the stopped-service/revision precheck, the migration container invocation is exactly:

~~~bash
docker compose run --rm --no-deps \
  -e "CALENDAR_AAD_ROLLOUT_STATE=/backups/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json" \
  -e "CALENDAR_AAD_IMMUTABLE_IMAGE_ID=${CALENDAR_AAD_IMMUTABLE_IMAGE_ID}" \
  -v "${BACKUP_DIR}:/backups:ro" --entrypoint /bin/sh migration -ec \
  'export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_migrate_0019'
~~~

Implement `just calendar-aad-resync-0019` with no pair/user argument. Before starting its one-off container,
the host recipe must inspect Compose state and fail when `caddy`, `api`, `worker`, or `scheduler` is running. It
also proves the database revision is exactly 0019, validates the exact rollout state/basename, and checks
the typed rollout guard plus actual image content ID. It uses the already selected immutable v2 image and the
`worker` service's existing Secret/environment mounts, but
uses `--no-deps --entrypoint /bin/sh` and a fixed command that reads only
`/run/secrets/app_database_password` into `PGPASSWORD` before `exec uv run --no-sync python -m
ai_employee.cli.calendar_aad_0019`; PostgreSQL and Redis remain running, while no Taskiq process or general
queue consumer starts. A zero-marker run succeeds as an evidenced no-op. Any failed or remaining marker stops
the rollout before `post-resync` can pass.

After the stopped-service precheck, the recipe's container invocation is exactly:

~~~bash
docker compose run --rm --no-deps \
  -e "CALENDAR_AAD_ROLLOUT_STATE=/backups/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json" \
  -e "CALENDAR_AAD_IMMUTABLE_IMAGE_ID=${CALENDAR_AAD_IMMUTABLE_IMAGE_ID}" \
  -v "${BACKUP_DIR}:/backups:ro" --entrypoint /bin/sh worker -ec \
  'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_0019'
~~~

Implement `just calendar-aad-audit phase artifact` with exactly three accepted phases: `pre-migration`, `post-migration`, and `post-resync`. Treat `artifact` as the same validated backup path prefix, require the bound preflight state, and write a distinct `${artifact}.calendar-aad-${phase}.json` file for each phase. The script is read-only, obtains database access from the existing Compose secret boundary, validates `rollout_digest_v1`, the actual image content ID, and the typed rollout guard before DB reads and immediately before atomic artifact rename, creates phase artifacts with mode `0600`, and never prints or stores a DSN, event body, description/location plaintext, token, scope string, provider response, or raw `calendar_id`. Artifacts contain only schema/revision, `rollout_digest_v1`, earliest deadline, image/basename binding, row/ciphertext digests, locally hashed connection/scope identifiers, affected/connection count, and the content-free checks needed for comparison.

Every backup, including a change-window backup, first acquires the Task 16A target lifecycle advisory lock from
management database `postgres`, then holds fixed database-level session advisory
`BACKUP_LIFECYCLE_LOCK=(20260806, 274)` and then `${BACKUP_DIR}/.ai-employee-backup.lock` via `flock`. Keep both
from creation of a unique `.partial.<uuid>` staging tree and final-name conflict check through local publication,
remote copy, retention, and orphan cleanup. The database lock serializes the same target database across hosts; the
directory lock serializes local publication/cleanup even across different target databases. Both are required and
the acquisition order is fixed.

While the management lease remains live, on the same database lock-holder session acquire shared
`SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`, read the pre-Alembic revision, retain the shared lock throughout the full
`pg_dump`, read the post-revision, and require byte-identical revision plus current-session lock ownership. Only then
may this run finalize/publish the local manifest and release the shared lock. Task 16A's outer online migration
boundary holds the matching exclusive lock until commit/rollback, so backup and migration cannot overlap at commit.
No common MVCC snapshot is required; pre/post CAS and shared/exclusive lock are both mandatory. Revision drift,
missing lock ownership, connection loss, or a bypassed migration produces no final three-file group.

The manifest schema is `ai_employee.postgres_backup_manifest.v1` and stores safe dump basename, UTC `created_at`,
PostgreSQL server version, the CAS-proven Alembic revision, encrypted dump lowercase SHA-256/size, exact checksum
filename, resolved immutable backend image `sha256:` content ID, allowlisted content-free build/release metadata,
the deterministic content-free `restore_fingerprint_v1`, and the allowlisted image-internal executable digest map.
The release test builds the actual `backend/Dockerfile` from repository-root context, verifies every operations
entrypoint is copied into the immutable image, and rejects executable bind mounts. The manifest contains no DSN,
credential, token, content, raw provider identifier, or personal data. Dump, checksum and
manifest are `0600`; the checksum covers finalized dump and manifest by safe relative name, while the manifest repeats
dump digest/size and binds checksum filename. Generate all three only in this run's staging, cross-verify, and publish
final names with no-clobber semantics; rename the manifest last as the only publication marker. Any existing member
is an all-group collision. On failure, delete only current-run staging and final members this run proved it created;
never touch a conflicting/other-run member.

rclone uses a unique remote staging prefix, publishes dump/checksum and then final manifest, and reports success only
for a complete final group. Local/remote orphan grace is fixed at 3600 seconds. While both serialization locks remain
held, cleanup immediately rechecks mtime and final manifest and may remove only expired `.partial.<uuid>` staging or
an expired incomplete group with no final manifest. A complete group, recent incomplete group, live run, or another
run survives. Daily/weekly retention also preserves/deletes complete basename groups only.

For the change-window backup, `BACKUP_ARTIFACT_BASENAME` is an optional basename only: reject an empty value, path
separators, traversal, unsupported characters, or any already existing group member; append the normal encrypted-
dump/checksum/manifest suffixes inside `/backups`; and keep timestamp naming as the default for ordinary backups.
When this variable is set, `scripts/backup-postgres.sh` requires the exact adjacent preflight state, validates the
actual image content ID and typed rollout guard before `pg_dump` and before manifest-last publication, and fails
without a published group on expiry or zero-state drift. `justfiles/ops.just` passes only the validated basename/state
path and host-resolved image ID explicitly to the backup container without exposing Secret values.

The phase contract is fixed:

- `pre-migration` requires revision 0018, rejects every partial legacy AEAD triple, proves every affected `(connection_id, calendar_id)` has its exact non-directory Calendar cursor, connected owning connection, enabled `calendar.read`, both access/refresh credentials, and exact ProviderCalendar, records the same locally hashed pair/connection set and deadline as provider preflight for operator comparison, and writes a deterministic content-free baseline before any DDL/DML.
- `post-migration` requires revision 0019, proves event identity/non-AAD business-row summaries and ciphertext/nonce/key-version digests are unchanged, verifies the expected v1/NULL version marking, confirms directory state is unchanged, and shows that only the exact affected pair's cursor/freshness/error marker changed.
- `post-resync` proves all affected markers are cleared, cursor/freshness is restored, every non-empty field newly written during this recovery is v2, and reports the remaining historical v1 count without attempting to decrypt v1 content.

Keep generic `just restore <exact encrypted path>` as the disaster-recovery entry, but accept only a published
manifest-bound group. Before any owner Secret/service/connection or `pg_restore`, the host validates safe path and
basename, all three members, schema, digest/size, checksum filename, backup revision, content-free image metadata,
`restore_fingerprint_v1`, and the image-internal executable digest map.
In production it then resolves `EXTERNAL_WRITES_ENABLED`, `GOOGLE_WRITES_ENABLED` and
`MICROSOFT_WRITES_ENABLED` to exact `false`; rejects any running Caddy/API/Worker/Scheduler/migration/role-bootstrap,
general consumer, backup/retention job, write-credential one-off, or owner maintenance/general consumer; and requires
a second production-only exact confirmation after that state check. Development/test rejects a writer in the same
workspace unless an isolated target is explicitly proven.

Replace current app-role `backup` service reuse with one generic owner maintenance/restore service that does not
inherit `backend-common` or auto-run migration. Mount backup/artifact inputs read-only and only a distinct validated
`RESTORE_STATE_DIR` read-write. `.env.example` sets only the development/test host default
`./var/restore-state`; Compose mounts it at container `/var/lib/ai-employee/restore-state`, while production requires
an explicit absolute host directory/volume. Reject symlink, wrong owner/mode, group/world-write and resolved overlap;
directory/file modes are `0700`/`0600`, and state never contains database credentials, generated SQL, or other
Secrets. Its image-internal executor
derives the exact Task 16A low/high-bit digest vectors, acquires the
management lifecycle lock from `postgres`, then the nonblocking target lock and exclusive schema lifecycle lock,
and reads authoritative
`ai_employee.maintenance_gate`, `ai_employee.restore_call_authority`, and `ai_employee.restore_completion` directly
from `pg_db_role_setting`. Require one current-database/`setrole=0` row per present key and strict
gate/call/completion grammar; role override, duplicate, malformed phase/ordinal/backend/completion facts, lock
contention or nonmatching/`needs_attention` authority fails before database or projection writes. The same
lifecycle/target guard covers every migration/reset/drop/create, standalone role
bootstrap/init-db-roles, generic/sealed restore/verifier, backup, and owner one-off. Legacy conversion remains on its
disposable target/registry/scavenger path and never writes workspace restore catalog facts.

Freeze the same three-state admission matrix as Task 27D: pristine has all three facts absent with baseline ACL;
active has matching gate + non-completed call, absent completion, and revoked PUBLIC/app/retention CONNECT;
completed idle has gate absent, a required completed call, a matching zero-slot-bound completion GUC, and baseline
ACL. Every other tuple fails closed, while pristine must allow migration/role-bootstrap.

For a new generic attempt, pristine/completed idle is virtual `new` with frozen new attempt UUID, zero call/reopen
ordinals, database gate timestamp, and exact identity facts. Completed idle first validates the exact completed pair,
rebuilds/fsyncs its immutable mode-`0600` state-v4 archive, and carries the prior digest in the new call. One holder
transaction rechecks the exact prior tuple/archive digest, RESETs completion, atomically replaces absent/old completed
call with `gate_established`, sets the gate, and revokes PUBLIC/app/retention CONNECT. It writes no pre-restore gate or
failure audit; only catalog gate/call and the post-commit state-v4 projection persist.
Before that mutation, a no-gate request read-only compares current full revision/fingerprint to manifest expected
post; exact equality returns `restore_already_applied` with database writes/audit/GUC/psql/pg_restore all zero and
publishes only deterministic canonical local evidence under the kind-aware
`<target>.<kind>.<source>.already-applied.json` path with no-clobber, byte-identical-idempotent semantics. A resumed matching
`gate_established` attempt may use the same exact-post evidence to transition directly to `restore_succeeded`
without psql/pg_restore or a separate already-applied audit, then still runs grants/verifier/reopen.
`needs_attention` is terminal. After commit it terminates existing non-owner sessions and proves denial. The control
sessions hold management/target/schema locks across psql backend registration, pg_restore-to-psql stream execution,
backend-exit reconcile, grant callbacks, verifier, and final reopen. Control-session loss yields unknown but is not
psql-exit proof; a new invocation must reacquire the locks and reconcile the exact PID/backend-start/database/role/
`PGAPPNAME` facts in authority before fingerprint reads. `pg_stat_activity` alone is insufficient.

The three database-wide catalog facts are the only cross-host restore facts. Persist only a v4 projection at
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>/<attempt_uuid>.json`, mode `0600`, parent `0700`; the state directory must be
outside read-only backup/artifact trees. It mirrors the complete content-free authority/result through same-directory
temp, mode check, file/directory fsync and atomic rename, but never authorizes transition/spawn/reopen. Missing,
corrupt or stale projection is rebuilt from gate/call/completion facts. Completed state requires the non-optional
completed call + matching completion GUC, gate absence, and baseline ACL on any host; only completion audit/local
projection may be absent after retention/privacy deletion or host loss. The final projection becomes an immutable
archive before the next attempt but never replaces the catalog call. Insufficient/mixed catalog facts are
reported with a `needs_attention` disposition and only persisted when the frozen graph permits that edge; they are
never treated as “not spawned”.

Freeze the exact phase graph:

```text
new → gate_established
gate_established → restore_backend_starting | restore_succeeded  # 后者只允许 exact post evidence，零 psql/pg_restore
restore_backend_starting → restore_backend_ready | needs_attention
restore_backend_ready → restore_started | restore_not_applied | needs_attention
restore_started → restore_succeeded | restore_outcome_unknown | restore_not_applied | needs_attention
restore_outcome_unknown → restore_succeeded | restore_not_applied | needs_attention
restore_not_applied → restore_backend_starting # 显式新 call_ordinal，且先证明 exact pre-state
restore_succeeded → grants_succeeded
grants_succeeded → verified
verified → reopen_committing
reopen_committing → completed | reopen_not_applied | needs_attention
reopen_not_applied → reopen_committing         # 新 reopen_ordinal
```

Every transition CASes exact old catalog value plus attempt UUID, expected phase and `call_ordinal`; reopen also
CASes independent `reopen_ordinal`. Terminals, jumps, regressions and self-transitions are rejected. Call authority
uses the exact ASCII-only, 21-delimiter, maximum-1133-byte `restore-call:v4` grammar from the spec and persists prior
completion digest, gate/call timeline, exact psql backend identity, expected/observed facts, completed_at, and the
completion authority digest. Completion GUC is exactly 86 ASCII bytes. Duplicate/role-specific/malformed catalog
settings and invalid phase-dependent dash/value combinations fail closed. Field monotonicity applies within one
`call_ordinal`: call-start/pre frozen at starting persist; starting→ready may fill backend once and later phases
preserve it; exact-post may fill observed once and successors preserve it. No populated fact may be cleared or
rewritten within the ordinal. The only controlled cross-ordinal clearing is the legal new-ordinal starting CAS,
which requires ordinal+1, a fresh database
call-start, exact-current pre re-freeze, backend PID/start reset and observed/completed starting shape; prior ordinal
backend facts cannot be inherited. The direct exact-post ordinal-0 shape remains separate.

For every real ordinal, first prove current full revision/fingerprint exactly equals the frozen pre-state. In one
owner transaction CAS exact old authority
`gate_established|restore_not_applied → restore_backend_starting`, increment the never-reused ordinal by exactly one,
retain `reopen_ordinal`, write a fresh database `call_started_at`, reset backend PID/start, re-freeze pre facts from
that exact current read, and reset observed/completed fields to the starting shape. A `restore_not_applied` retry
must not copy prior backend facts; within the new ordinal, ready/started/outcome preserve its call-start/pre/backend
facts. The direct exact-post ordinal-0 shape is unchanged. The controlled Python executor reads the long-lived
bootstrap owner Secret only to spawn a fixed
target-database, role `ai_employee_owner`, exact
`PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>`
`psql --set=ON_ERROR_STOP=1 --single-transaction` consumer. The controller alone holds the anonymous-pipe write end;
psql receives no SQL while connecting. The holder session requires exactly one matching backend, proves PID and UTC
six-microsecond `backend_start` are newer than the spawn fence, then CASes
`restore_backend_starting → restore_backend_ready` and persists the identity. Only with matching gate/authority/
backend/pipe may it CAS `restore_backend_ready → restore_started` and fsync v4 projection; SQL bytes and pg_restore
calls are zero before this point.

After started, write the transaction-local deferred completion-guard prelude, then spawn credential-free
`pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>` with all `PG*`/DSN/
Secret values removed. Stream stdout in bounded chunks into psql without shell pipeline, SQL persistence, or SQL
logging. Send the guard trailer only after generator EOF and exit zero; controller crash, missing trailer, either
child failure, or pipe error rolls back the psql transaction. Spawn-before-visible and ready-before-feed crashes
execute zero SQL; mid-stream crash rolls back. Before fingerprint/not-applied/new ordinal,
ready/started/unknown phases must prove the authority-bound PID/backend-start/database/role/application backend has
exited. Starting without a registered backend may continue only when the original still-live local controller proves
the exact child/pipe identity and SQL-byte-zero, and then only to ready. A different controller/host or missing/
unverifiable local projection CASes `needs_attention` (or persists only that disposition when CAS is unsafe), keeps
`pg_restore_calls == 0`, and cannot infer not-applied or allocate an ordinal. Exact post means success, exact pre from
registered-backend phases means `restore_not_applied`, other means `needs_attention`; starting manual disposition
requires operator process verification/termination plus explicit forward-fix. Only operator-approved new ordinal
from legal `restore_not_applied` plus fresh exact-pre proof can retry.

Only a proven successful restore invokes object/schema grants on the already-held session. A definite grant rollback
keeps `restore_succeeded`; verifier failure keeps `grants_succeeded`, with bounded retry and no self-transition. The generic verifier
then performs `SET ROLE ai_employee_app`, `BEGIN READ ONLY`, session/current-user checks, SQLSTATE `25006`, and every
manifest-selected schema/revision/fingerprint/health/sample check before reopen. CAS `verified` to
`reopen_committing` with a new reopen ordinal. Freeze the exact `database.restore.completed` event,
`ai_employee.database_restore_completed.v1` key set, database UTC timestamp, canonical proof bytes,
safe `gate_established_at`/final-call/backend timeline plus expected/observed revision/fingerprint fields,
`completion_authority_digest_v1`, and ordinary BigInteger audit storage identity from the spec. Construct the full
completed `restore-call:v4` with a 64-zero digest slot, hash every byte under the frozen domain, then store the digest
in both the call slot and completion GUC; verification must re-zero and recompute. The final transaction
first requires exactly one `users` row, active or M1-retained inactive anonymized, and fails closed on zero/multiple
rows. It CASes authority to completed including the exact digest and inserts one audit with
`user_id=<sole users.id>`, `task_id=NULL`, `event_type=database.restore.completed`, `actor_type=system`,
`actor_id=database_restore`, database-generated `created_at`, and exact versioned metadata. It then sets
`ai_employee.restore_completion=restore_completion:v1:<digest>`, resets gate, restores only app/retention CONNECT,
and leaves PUBLIC revoked. Audit ID does not enter authority. A pre-existing duplicate attempt/digest audit, an INSERT
count other than one, extra/missing key, or digest/value mismatch rolls back at commit time; later ordinary retention
or all-data deletion may remove the audit. On ACK loss, close sessions, reacquire lifecycle/target locks, and read
call + completion: a valid mutually bound completed pair + gate absent + baseline ACL is final even if audit/local
projection is gone; missing completed call is corruption, not a diagnostic omission. Absent GUC +
unchanged `reopen_committing` + active gate + revoked ACL CASes by exact old value to `reopen_not_applied`; only an
explicit new reopen ordinal may retry. An active attempt can never retain a prior completion value. Mixed GUC/gate/
call/ACL facts are terminal `needs_attention`; pristine all-absent + baseline remains legal. General services remain
stopped for operator restart. Tests include the full `pristine → active → completed → next active` matrix, prior-pair
archive/reset/replace, and a crash immediately after final commit.

Reject a pre-manifest backup in production. Implement only
`just restore-legacy-to-isolated file output_basename` for `APP_ENV=development|test`, backed by dedicated
`scripts/convert-legacy-backup.sh` and Compose isolated `legacy-conversion-postgres`/`legacy-backup-converter`
services under profile `legacy-conversion`. Validate the exact regular
legacy dump/checksum, safe output basename, and no final-group conflict. Before any resource, fsync+atomically
publish the mode-`0600` attempt registry with deterministic project/container/network/volume names, controlled
Secret path, source binding, UTC creation time, and exact labels. Create project
`aiemployee-legacy-<32-lowercase-hex-uuid>`, project-private `internal: true` network, ephemeral volume, and generated
`0600` temporary database Secret at the registered path without outputting it; label every resource and attach no
workspace/production network, volume, host, or database. Restore only there, read actual Alembic revision and health
in read-only mode, reject unknown/unsupported revision, then invoke the ordinary locked backup path against the
isolated database to publish the requested new manifest-bound group. Run `legacy-backup-scavenge` before conversion.
A trap on success, error, or catchable signal runs profile-scoped Compose `down --volumes --remove-orphans`, deletes
the Secret/registry, and verifies no labeled resource remains. The separate scavenger handles SIGKILL/daemon/host
crash after fixed grace by acquiring a per-attempt nonblocking lock, matching registry to labels, and requiring no
active container; uncertainty preserves resources and another active run. It never fabricates a manifest,
changes/stamps a revision, or calls generic
target/sealed restore. Generic, sealed, and legacy recipe/service/script/fixture/test paths are mutually isolated.

Implement the revision-0018 deterministic verifier as a sealed-specific callback, not a separate post-reopen
recipe/service. After the sealed restore and grants, the same target-lock holder executes
`SET ROLE ai_employee_app`, `BEGIN READ ONLY`, SQLSTATE `25006` DML rejection, and compares affected/connection
counts, hashed connection/pair sets, event/identity/cursor/ciphertext digests, credential-row presence, and
ProviderCalendar ownership with the bound `pre-migration` artifact. Every difference fails before atomic reopen.
Only the required completed call + expected completion GUC with a valid zero-slot digest, gate absence, and baseline
ACL authorizes the operator to select the recorded original 0018-compatible image and run health checks. Ordinary
retention or all-data deletion may remove the audit without invalidating the catalog pair; the call itself is not
optional diagnostic evidence.

Add `just calendar-aad-restore-0018 <exact encrypted path>` with a distinct
`calendar-aad-restore-0018` service in the operations profile; generic `just restore` is explicitly forbidden for
this sealed-window path. In production the sealed recipe still requires `APP_ENV=production`,
`ALLOW_PRODUCTION_RESTORE=yes`, the configured `BACKUP_DIR` volume, `[confirm]`, and the second exact-path
confirmation. Before Compose can read any owner Secret, open an owner database connection, decrypt the dump, or
invoke `pg_restore`, the host recipe performs a pure file/local-image guard: independently validate the safe basename and exact
path, encrypted dump plus adjacent checksum and versioned manifest, original preflight and `pre-migration` artifacts, their canonical
schema/digests/basename, immutable image binding, and allowlisted image-internal executable digests; then resolve the
Compose-selected local image to its actual content ID and reject executable bind mounts. A missing/wrong image or
script, moved tag, checksum failure, bind mount, or artifact/image mismatch exits with zero owner
Secret reads, zero owner connections, zero `pg_restore`, zero pull, and zero build calls.

Only after that guard succeeds does the recipe inject the matching exact `sha256:...` as internal
`CALENDAR_AAD_RESTORE_IMAGE_ID`. The `calendar-aad-restore-0018` service's `image:` directly references that variable; it must not
contain a tag or `build:` and must be invoked as
`docker compose --profile operations run --rm --pull never ... calendar-aad-restore-0018`. It never inherits `backend-common`, has
only `postgres: service_healthy` under `depends_on`, fixes only `PGHOST` and `PGDATABASE`, explicitly forbids a
service-wide `PGUSER=ai_employee_owner`/`PGPASSWORD`, and mounts `backup_passphrase`, the controlled `/backups` volume plus bound
preflight/`pre-migration` artifacts read-only, a distinct `RESTORE_STATE_DIR` volume read-write,
`app_database_password`, `retention_database_password`, and the owner Secret as controller-only files;
it does not mount or start Redis, migration, API, Worker, Scheduler, or Caddy.

The container entrypoint does not pre-export owner `PGPASSWORD`. The independent
`scripts/restore-calendar-aad-0018.sh` first revalidates
the artifact files, exact `CALENDAR_AAD_RESTORE_IMAGE_ID` `sha256:` shape, image-internal script digests, and equality
with the preflight image binding. Only then may it read `/run/secrets/postgres_bootstrap_password`, set the owner
credential in controlled memory, verify/decrypt the dump, and invoke the common maintenance executor with
`kind=sealed_0018`. The executor owns management/target/exclusive-schema locks, gate/call/completion facts, state-v4
projection, exact psql backend registration, credential-free pg_restore-to-psql streaming with deferred completion
guard, authority-bound backend-exit reconcile, same-session grants/verifier, and
ordinal-aware final reopen; it never invokes standalone `init-db-roles.sh` or a
post-reopen verifier. Generic `scripts/restore-postgres.sh` contains no sealed artifact/image logic. Task 27D must not
add a migration for the refresh fence,
downgrade, direct SQL, marker skip, new Task 18 command, or an OAuth-only service. Because all general services and
write switches remain stopped from preflight through verification, and the backup is after confirmed credential
rotation but before 0019, the whole-database restore loses no business write and preserves the current refresh
credential.

`scripts/test-calendar-aad-0019-audit.sh` must use only the Task 13 synthetic PostgreSQL database and cover two connections sharing one `calendar_id`, a single affected connection, exact-pair marker isolation, missing cursor/disconnected connection/non-enabled capability/missing access or refresh credential/missing ProviderCalendar failures, all three typed-rollout-guarded phases, sealed restored-0018 deterministic comparison through the holder-session `SET ROLE` + `BEGIN READ ONLY` callback, same-connection DML rejection, `0600` permissions, and forbidden-output scans. `scripts/test-m2-release.sh` must invoke this script as an explicit subprocess; Task 27D initially supplies the exact Task13 `TEST_DATABASE_URL` on that call, while Task 30 validates and exports the same value once at script start so the audit and every other child inherit it. `just ci` is not a substitute. Tasks 27C/27D are not complete until the provider-preflight integration test, recovery integration test, deadline/dual-restore integration, audit test, and tooling test prove all exact one-off entries above; they must not invent a direct SQL mutation, use a directory/full-account sync, start the ordinary Scheduler/Worker, contact a live provider, or describe an unimplemented command as already available.
`backend/tests/integration/operations/test_postgres_backup_restore.py` and
`scripts/test-legacy-backup-conversion.sh` are additional mandatory Task 27D gates; neither may be hidden inside the
calendar deadline test or replaced by static documentation review.

Extend `scripts/test-deployment.sh` to assert write switches default off, Microsoft secret-file mounts, no host
publication of internal metrics ports, no secret values in rendered Compose, a distinct generic owner maintenance/
restore service, image-internal operations executables with no bind-mounted override, isolated legacy PostgreSQL/
conversion services on a project-private internal network/ephemeral volume/registered Secret, no migration/general-service inheritance, and the full
sealed restore service contract: operations profile, no `backend-common`, PostgreSQL-only healthy dependency, fixed
controller bootstrap owner role, bootstrap/app/retention/backup Secret mounts, bound artifact/read-only backup volumes, internally injected
exact `sha256:` `image:`, distinct writable `RESTORE_STATE_DIR` outside those read-only trees, no `build:`, fixed
`--pull never`, container pre-Secret artifact/image guard, fixed management→target→schema wrapper order, all atomic
psql flags plus exact `PGAPPNAME`, credential-free pg_restore generator flags, controller-owned stream/completion
guard, starting/ready/started/backend-exit reconcile, and holder-session
sealed verifier before ordinal-aware atomic reopen. Assert generic manifest
metadata does not force sealed exact-image requirements and generic/sealed/legacy services/scripts/recipes/fixtures/
verifiers/tests are distinct.

##### Combined verification reference

Run the digest, identity, coordinator, and exact preflight/recovery integrations first:

~~~bash
uv run --project backend pytest \
  backend/tests/unit/application/test_calendar_aad_digests.py \
  backend/tests/unit/application/test_oauth_refresh_identity.py \
  backend/tests/unit/application/test_oauth_refresh_coordinator.py \
  backend/tests/unit/application/test_connection_capability_use_cases.py \
  -q
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test \
uv run --project backend pytest \
  backend/tests/integration/m2/test_connection_capability_repository.py \
  backend/tests/integration/m2/test_credential_rotation_repository.py \
  backend/tests/integration/m2/test_oauth_refresh_coordinator.py \
  backend/tests/integration/api/test_connections.py \
  backend/tests/integration/google/test_oauth_flow.py \
  backend/tests/integration/google/test_gmail_sync.py \
  backend/tests/integration/google/test_calendar_sync.py \
  backend/tests/integration/microsoft/test_oauth_flow.py \
  backend/tests/integration/microsoft/test_mail_sync.py \
  backend/tests/integration/microsoft/test_calendar_sync.py \
  backend/tests/integration/operations/test_calendar_aad_0019_preflight.py \
  backend/tests/integration/operations/test_calendar_aad_0019_recovery.py \
  backend/tests/integration/operations/test_postgres_backup_restore.py \
  backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py \
  -q
~~~

Expected: PASS with independent fixed canonical-byte/base64/hash vectors for
`credential_snapshot_digest_v1`/`refresh_credential_snapshot_digest_v1`/`rollout_digest_v1` plus the fixed
HKDF/HMAC `refresh_token_identity_v1` vector; coordinator claim ordering, dual-Worker single-call,
response-after-lock-loss/CAS-miss and Taskiq/`TransientProviderError` zero-call cases; versioned
confirmed/unsatisfied/replacement result-union ACK reconciliation with actual commit permanently closing only the
allowed attempts, actual rollback unresolved, unsatisfied preserving original fence and replacement consuming it,
strict confirmed disposition/flag/equality and replacement old != new/changed=true parsing with mismatch rejection,
and no guessed result or extra provider/code call; later valid refresh/reauth before reconcile leaves historical
attempt closed and uses current readiness. changed=false later access-only refresh/same-plaintext re-encryption is
non-conflicting, while changed=true A→B→A, missing-row, invalid-AEAD/ownership, or invalid-state conflict returns
`oauth_credential_state_conflict` at zero calls; automatic
missing/same/different confirmed handling with complete G/G/G proof, Google access-only confirmed and byte-preserved
refresh row, missing/same old == new/changed=false, different old != new/changed=true without old-fence consumption,
and stale-CAS rejection; explicit
progressive recovery start transaction with OAuthAttempt + recovery-started event, repeated attempts where the first
denial/missing/same/known-failure unsatisfied result atomically converges only requested capabilities from
`authorizing` to `action_required`, preserves actual scopes/last-verified facts and the original fence, contains no
credential post/expiry fields, stale `T` is a capability no-op, and a later new-attempt different-token callback
atomically commits credential/scope/capability plus complete old != new/changed=true F/S/T replacement proof while target generation stays
`T`; Google/Microsoft any-valid-error one-time callback, code/error mutual exclusion, pre-consumption shape rejection,
unknown-error consumption then replay rejection, safe classification for `oauth_authorization_failed`,
`microsoft_admin_consent_required`, and `microsoft_reauthorization_required`, raw-error redaction, and targetless fenced-identity
pre-save blocking; `F=S`/`S>F`, stale/concurrent target, missing/empty/same refresh, disconnected/old-plaintext-
unavailable, provider/network/lease/CAS failure negatives, complete proof fields and strict timestamps; post-
consumption `action_required` reauthorization and later-generation monotonicity; fixed-root-key identity semantics with
same-plaintext re-encryption preserving identity/changed=false and changed=true A→B→A conflict; and zero replay
of the original D0 preflight refresh; 0018
local/provider recoverability proof; revision-global lease across same/different basenames;
provider-before-started-call count zero; exactly one proactive refresh per newly fenced affected connection;
attempt-bound dual-digest generic started/confirmed atomicity with required identity/disposition/post-digest/
`refresh_identity_changed`/persisted-expiry/deadline-candidate fields and schema-mismatch rejection; unresolved
cross-basename blocking; repeated explicit recovery with
started/unsatisfied/replacement facts, targetless blocked audit that does not close the fence, and
missing/empty/same-plaintext/access-only negatives;
closing-result-before-artifact crash recovery with exact-post or later-current readiness and no second refresh; lock contention/loss,
network unknown, `invalid_grant`, malformed response, scope shrink, credential snapshot CAS, definite rollback and
commit-ACK-loss result-union reconciliation with zero duplicate provider calls; access-only/undecryptable refresh rejection;
original earliest-expiry-minus-900-seconds artifact deadline plus current-access-row effective-deadline tightening,
including ACK-lost shorter-expiry and non-extending longer-expiry races; Fake and HTTP-mocked real-adapter exact-scope probes; CAS-only
AEAD rotation;
zero Calendar fact mutation;
ordinal-aware marker-only task creation; unconditional active-attempt reuse; one TaskRun/Outbox per new ordinal
under replay/concurrency; explicit failed/cancelled retry; immutable old terminal tasks; deadline fail-closed at
every critical boundary; exact zero/no-deadline rechecks and immutable-image mismatch rejection; fixed database
backup lock plus `${BACKUP_DIR}` flock, unique `.partial.<uuid>`/remote staging, 3600-second lock-held orphan
rechecks, shared schema-lifecycle lock with pre/post revision CAS and backup-vs-migration exclusion; generic
dump/checksum/versioned-manifest `0600` cross-binding, no-clobber/manifest-last local/remote publication, whole-group
conflict/retention; image-internal runtime scripts/digests with no executable bind mount; owner restore of ordinary
0019 backups without rollout artifacts; production write-switch/service/second-confirmation gates followed by one
management lifecycle then target/exclusive-schema locks and durable gate/call/completion facts shared by every lifecycle/owner entry;
live-holder/crashed-holder/nonmatching zero-write plus reset zero-drop/create proof; PUBLIC/app/retention CONNECT
revocation, existing writer termination, post-check new-connection denial, read-only artifacts plus separate
state-v4 projection, psql backend starting/ready/started registration, SQL-byte-zero before started, deferred-guard
credential-free pg_restore stream rollback, authority-bound backend-exit-before-fingerprint, complete phase/CAS/
ordinal graph,
pre/post/inconsistent reconciliation, holder-session grants and `SET ROLE ai_employee_app`/`BEGIN READ ONLY`/
SQLSTATE `25006` verifier, complete-call zero-slot digest plus fixed-outer-field ordinary-ID exact metadata/timeline
insertion, gate-reset/minimal-CONNECT atomic reopen and completed-call/completion-GUC/gate/ACL ACK-lost
`reopen_not_applied` reconciliation; no pre-restore gate audit; full pristine/active/completed/next-active matrix;
same-workspace development/test rejection;
durable-registry/labeled/scavenged legacy conversion without manifest fabrication; app-role restore denial;
artifact-distinct sealed basename/dump/checksum/manifest/preflight/`pre-migration`/image/script mismatch with zero
owner and `pg_restore` calls; exact `sha256:` image, no build/pull, `--pull never`, pre-Secret container guard; and
sealed reuse of the same restore primitive with holder-session revision-0018 verification; zero
directory reads; exact-pair provider isolation; marker-aware final CAS; unrelated queue preservation; and no
trusted-write facts or calls.

Run: `TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py backend/tests/integration/retention/test_role_permissions.py backend/tests/unit/test_init_db_roles_script.py -q`

Expected: PASS, including CalendarEvent description/location four-column atomic cleanup, an unresolved started
fence surviving the 365-day cutoff with next-invocation provider calls still zero, user-isolated ordinary audit
cleanup, safe automatic-success/schema-and-capability-transition-bound unsatisfied-recovery/successful-recovery group
cleanup through the same strict disposition/flag/equality result parser without later credential-lineage matching,
maximum group `created_at < cutoff`, an old started plus newer result remaining protected,
ordinary-cutoff deletion of an old completion audit while the completed call + completion GUC pair remains authoritative,
completed admission/ACK succeeding from that pair after deletion, all-data deletion removing the completion audit while
retaining the sole inactive anonymized user row, and a later restore audit binding that row; malformed completion GUC
or missing/mismatched completed call still fails closed only at the restore boundary,
access-only/automatic-different-token/physical-snapshot rotation remaining blocked, retention-versus-confirmed/
recovery-result/consumption lock/CAS races, app-role DDL/restore denial,
post-restore app/retention grants, and
Secret-file-only role bootstrap.

Run: `TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test bash scripts/test-calendar-aad-0019-audit.sh`

Expected: PASS for the three typed-rollout-guarded audit phases, exact-pair isolation, all local recoverability
failures, zero-state drift rejection, restored-0018 comparison, image binding, artifact mode/content, and
sensitive-output scan using only synthetic data. `scripts/test-m2-release.sh` must execute this audit command
directly; Task 27D passes the exact Task13 value on the call, and Task 30 replaces per-command prefixes with a
validated script-wide export inherited by this subprocess and every other release child.

Run: `bash scripts/test-legacy-backup-conversion.sh`

Expected: PASS for development/test-only exact recipe signature, safe output basename and conflict rejection,
fsynced pre-resource registry, unique labeled Compose project/internal network/ephemeral volume/`0600` registered
Secret, actual revision/health read, ordinary manifest-bound output, success/failure cleanup, SIGKILL/daemon/host-
crash scavenging with active-run preservation, workspace/production target isolation, and forbidden output.

Run: `bash scripts/test-deployment.sh`

Expected: PASS, including immutable-image copies of every operations executable and no executable bind mount;
distinct generic owner maintenance/restore with same-session role verifier and no 0019 artifact dependency; common
management/target lock and gate/call/completion admission, read-only artifact mounts plus separate state volume,
psql backend registration plus deferred-guard pg_restore SQL stream/backend-exit/ordinal reconcile;
labeled/scavenged legacy isolation; and the sealed PostgreSQL-only exact-image/
script guard followed by the same generic/sealed restore executor. Generic, sealed, and legacy validators/artifacts
remain separated.

Run: `bash scripts/test-tooling.sh`

Expected: PASS, including safe explicit backup basename/state handling, collision/path rejection, start/pre-commit
typed rollout checks, host-resolved immutable-image binding, revision-global plus connection-scoped coordinator
leases and shared CAS boundary, durable generic automatic started/confirmed fence, repeated explicit recovery
started/unsatisfied/replacement gate, strict identity-change result parsing, targetless pre-save blocking, automatic
access-only changed=false confirmed and different-token changed=true/no-old-consumption cases, two-Worker single-call
and unknown/Taskiq zero-call cases,
closing-result/current-readiness crash resume, exact active-refresh preflight/guarded migration/three-phase audit/resync/restored-0018
recipes, revision/stopped-service gates for every no-argument one-off, the
generic manifest-bound `just backup`/`just restore` group publication, remote/retention behavior, production
global/local lock, schema-revision CAS, unique staging/orphan grace, write-switch/service/second-confirmation plus
management→target→schema lock order, three catalog facts, lifecycle-wrapped reset with no direct drop/create,
generic/sealed state-v4/psql backend starting-ready-started/deferred-guard stream/backend-exit/ordinal/
completion-GUC atomic-reopen gates, dedicated
registry-scavenged legacy conversion, and
owner-session verifier, plus the
independent owner-before-secret/image-guarded
`calendar-aad-restore-0018` service on the same generic/sealed restore primitive, explicit release-script audit
invocation, holder-session
app-role read-only enforcement, four-entry real-Uvicorn OAuth-query canary, zero Calendar mutation during preflight, and no
Taskiq/general queue/OAuth-only/migration service startup.

Run:

~~~bash
uv run --project backend ruff check backend/src backend/tests
uv run --project backend ruff format --check backend/src backend/tests
uv run --project backend mypy backend/src
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test just check
git diff --check
~~~

Expected: PASS with no real-provider access.

Inspect the complete `docs/operations.md` plus `docs/acceptance-checklist.md` diff for the full Caddy/API/Worker/Scheduler drain, four-entry access-log shutdown/canary, revision-global session lease before artifact/provider access, required decryptable/usable refresh token, complete credential snapshot CAS, one proactive refresh per connection, original artifact deadline plus current-access-row effective-deadline tightening, deadline guards at every CLI/recipe critical boundary, Task 16A frozen migration guard with Task 27C injection-only ownership, pre-backup 0018 exact-pair provider probes and failure disposition, post-migration stopped-service interval, unconditional active-attempt reuse plus failed/cancelled-only ordinal allocation, exact marker-only task execution, three-phase artifact contract, post-resync-before-start gate, ordinary dump/checksum/versioned-manifest/fingerprint/script-digest publication, production generic stopped-write/second-confirmation boundaries, low/high-bit target vectors and management→target→schema lifecycle admission, reset holder-crash zero drop/create, manifest-bound generic/sealed closed pristine/active/completed catalog matrix with `restore-call:v4` length/parser rules, required completed-call/completion-GUC pair, prior-pair state-v4 archive, read-only mounts, kind-aware no-clobber DB-zero-write local already-applied evidence, psql backend registration plus deferred-guard credential-free pg_restore stream/backend-exit ordinal reconcile, active-attempt completion RESET, no pre-restore gate audit, and pair/gate/ACL atomic reopen; durable-registry legacy scavenging; sealed extra artifact/image/script guard on the same restore primitive; operation-specific same-session verification; explicit release-script audit invocation; obsolete-0019 non-production reset versus production stop; and the normal 0019-compatible rollback floor. This document and synthetic-script review is required release-contract evidence, but neither it nor an unexecuted production recipe is evidence that a production 0019 rollout has run.
The backup/restore review must additionally show fixed global/local backup locks, unique run/remote staging,
3600-second lock-held orphan rechecks, Task 16A schema-lifecycle exclusive versus backup shared lock with pre/post
revision CAS, production management/target/exclusive-schema locks plus three catalog facts and CONNECT revocation/
termination/new-connection denial, state-v4 projection and psql backend registration/deferred-guard SQL stream/
backend-exit/ordinal restore reconcile, cross-host starting fail-closed without local child/pipe identity,
owner-session `SET ROLE ai_employee_app` verification, active-attempt completion RESET, completion-GUC plus
required completed-call zero-slot binding, prior-pair archive, no pre-restore gate audit, fixed-outer-field ordinary-ID
exact metadata/timeline/digest/gate-reset/ACL atomic reopen including `reopen_not_applied`, ordinary retention/privacy
deletion of completion audits without weakening completed-call/completion-GUC pair authority, and the
exact legacy registry/labels/scavenger contract. Sealed uses a verifier callback on the holder
session; generic/sealed/legacy artifacts and preconditions cannot cross paths.
The review must explicitly include existing-AuditEvent attempt-bound refresh started/confirmed fences,
`rollout_digest_v1` plus full and refresh credential snapshot digests and the independent HKDF/HMAC identity vector,
shared `OAuthRefreshCoordinator` claims, cross-basename unresolved blocking,
required confirmed post-digests/persisted-expiry/deadline-candidate fields, fresh-session versioned result-union
reconcile versus definite rollback for confirmed/unsatisfied/replacement, strict disposition/flag/equality parsing,
exact automatic/recovery closure and original-fence consumption, later-valid-refresh/reauth current-readiness races,
changed=false non-conflict and changed=true A→B→A/invalid-state `oauth_credential_state_conflict` zero-call
blocking, callback `compare_digest` plus atomic F/S/T `progressive_recovery`, requested-capability
`authorizing→action_required` unsatisfied convergence without credential post/expiry, stale-`T` no-op, new-attempt
retry, classified Google/Microsoft error consumption/redaction/replay handling, targetless fenced-identity pre-save block, and automatic
G/G/G missing/same/different confirmed handling without old-fence consumption, shared full-row/generation/identity
CAS, single-consumer recovery races, target generation preserved, stale/concurrent callback and disconnect-without-
consumption negatives, post-consumption capability/generation changes, safe callback classification preserving
`microsoft_admin_consent_required`/`microsoft_reauthorization_required` with `oauth_authorization_failed` fallback,
fixed-root-key same-plaintext changed=false identity plus changed=true A→B→A rollback rejection, cross-cutoff fence
retention through the same result union without later credential lineage and with whole-group cutoff, closing-result/
current-readiness crash resume, generic manifest-before-owner and stopped-write evidence, legacy isolation without
manifest fabrication, sealed host owner-before-secret artifact/image guard, exact
`sha256:`/`--pull never` restore, and verifier `PGOPTIONS` + first-action
`BEGIN READ ONLY` + SQLSTATE `25006` evidence.

##### Retired combined staging inventory

The former 65-file staging command is intentionally removed. Execute and commit only the independent Task
27A, 27B, 27C, 27D, and 27E file lists above; no combined Task 27 commit is valid.

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
- Modify: `scripts/test-m2-release.sh` — at the start, before any subprocess, accept an externally supplied
  `TEST_DATABASE_URL` only when it exactly matches
  `postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test`, otherwise
  fail closed; when absent, set that same constant, then export it once for every child command. Expand Task 27D's
  explicit calendar-AAD audit wrapper into the full release gate while retaining a direct
  `bash scripts/test-calendar-aad-0019-audit.sh` subprocess after the export. Add three fixed v1 digest vectors and
  the fixed `refresh_token_identity_v1` HKDF/HMAC vector,
  automatic coordinator/result-union recovery, four-entry real-Uvicorn access-log canary, Task 16A migration
  zero-bootstrap/typed-guard/schema-lifecycle-lock paths, current-expiry effective-deadline tightening,
  cross-cutoff OAuth fence plus ordinary completion-audit retention/privacy deletion with independent completion-GUC
  authority, serialized manifest backup/revision CAS,
  immutable operations image, fixed low/high-bit target vectors,
  management→target→schema lifecycle/reset boundary, database gate/call/completion facts, generic/sealed
  state-v4 psql backend registration/deferred-guard pg_restore SQL stream/backend-exit/same-ordinal fact preservation/
  controlled new-ordinal fresh-call-start/exact-current-pre/backend-reset/completion-GUC
  atomic-reopen restore, registry-scavenged legacy
  conversion, plus artifact-distinct sealed exact-image restore on the same restore primitive; it never
  performs production migration or restore.
- Modify: `backend/tests/integration/db/test_migrations.py` — release evidence for fresh/zero/missing/Fake/final
  0019 guard paths and the outer schema-lifecycle exclusive lock.
- Modify: `backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py` — release canary for all
  four launch entries and persisted sanitized audit.
- Modify: `scripts/test-tooling.sh` — release-wrapper command/order/environment assertions, including deterministic
  Task13 selection when the environment is absent, exact-match-only external override, rejection of other and
  production-like DSNs before any child call, and inheritance by the explicit audit plus every pytest/shell command.
- Create: `scripts/verify-m2-sensitive-output.py`
- Create: `docs/releases/2026-08-06-m2-release-evidence.md` — actual provider matrix and content-free 0019
  digest/HKDF-HMAC vectors; migration zero/guard paths; automatic/recovery result union; four-entry access-log
  canary; current-expiry effective-deadline tightening; ordinal recovery; fixed Task13 release-test environment;
  backup global/local/schema locks and orphan races; immutable operations image; generic manifest management/target
  locks, gate/call/completion facts, state-v4 psql backend registration/deferred-guard SQL stream/backend-exit/ordinal
  reconcile, same-ordinal fact preservation and controlled new-ordinal fresh-call-start/exact-current-pre/backend
  reset, plus
  completion-GUC atomic reopen; registry-scavenged legacy
  conversion; and sealed exact-image shared-restore-primitive evidence.
- Modify: `docs/acceptance-checklist.md` — require strict result-union/CAS/recovery evidence, frozen migration
  guard paths, sanitized callback and real access-log canary, durable fence retention, current-expiry deadline
  tightening, fixed release-test database inheritance, explicit audit invocation, manifest-bound generic restore
  with management/target locks, gate/call/completion facts, read-only artifact plus separate state-v4 projection,
  psql backend registration/deferred-guard SQL stream/backend-exit/ordinal reconcile, same-ordinal fact preservation,
  controlled new-ordinal fresh-call-start/exact-current-pre/backend reset, completion-GUC atomic reopen and
  owner-session role-switched
  verification, plus sealed owner-before-secret exact-image/script guard on the same generic/sealed restore primitive
  before release acceptance.
- Modify: `README.md`

- [ ] **Step 1: Write the failing crash matrix and Playwright flows**

The backend fault test must parameterize these exact crash points for every one of the four actions: before claim, after claim before request-start commit, after request start before response, after provider response before result commit, after result commit before queue acknowledgement, and during each reconciliation attempt. It must assert one ToolExecution, zero or one provider write according to the crash point, and final convergence to success, confirmed failure, or needs-attention without a fabricated result.

Playwright must cover Google and Microsoft Fake connections, new/reply/reply-all mail, calendar create/update/restore, editing conflicts, approval/rejection/expiry/invalidation, refresh/SSE reconnect, partial availability, capability reauthorization/admin consent, reconciliation, both manual outcomes, keyboard/focus/live region, and mobile layout.

Extend `scripts/test-tooling.sh` first so it requires the release wrapper to contain, in executable order, the
direct calendar-AAD audit command, migration fresh/zero/guard tests, real-Uvicorn canary, current-expiry deadline
tightening test, PostgreSQL backup/restore maintenance-gate integration, dedicated legacy conversion/scavenger shell test, and
generic/sealed/legacy separation test. The assertion must fail when audit is present only inside `just ci`. The same
test must run the wrapper with `TEST_DATABASE_URL` absent and prove every fake `just`,
pytest, audit and shell child sees the exact Task13 URL; run it again with the same external value and prove it is
accepted/exported; then use a different synthetic DSN and a production-like synthetic DSN and prove the wrapper
fails before the first child command. Per-command prefixes, a late export, or an audit/integration subprocess that
does not inherit the fixed value must fail the contract.

- [ ] **Step 2: Run focused fault/E2E tests and observe expected failures**

Run: `uv run --project backend pytest backend/tests/integration/faults/test_m2_write_recovery.py -q`

Expected: FAIL because the M2 fault scenarios are not registered.

Run: `pnpm --dir frontend test:e2e m2-actions.spec.ts`

Expected: FAIL because the M2 E2E support scenarios are absent.

Run: `bash scripts/test-tooling.sh`

Expected: FAIL because Task 27D's initial release wrapper does not yet execute the complete Task 30 evidence matrix.

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

`scripts/test-m2-release.sh` must begin with this fail-closed environment boundary before any subprocess:

~~~bash
#!/usr/bin/env bash
set -euo pipefail

readonly required_test_database_url='postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test'
if [[ -v TEST_DATABASE_URL && "${TEST_DATABASE_URL}" != "${required_test_database_url}" ]]; then
  printf 'M2 release test refused: TEST_DATABASE_URL must match the fixed Task13 database\n' >&2
  exit 1
fi
export TEST_DATABASE_URL="${required_test_database_url}"
~~~

After that single export, it must run in order, with every command inheriting the same environment:

~~~bash
just ci
bash scripts/test-calendar-aad-0019-audit.sh
uv run --project backend pytest backend/tests/integration/faults/test_m2_write_recovery.py -q
uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py -q
uv run --project backend pytest backend/tests/unit/application/test_calendar_aad_digests.py backend/tests/unit/application/test_oauth_refresh_identity.py backend/tests/unit/application/test_oauth_refresh_coordinator.py backend/tests/unit/application/test_connection_capability_use_cases.py -q
uv run --project backend pytest backend/tests/integration/m2/test_connection_capability_repository.py backend/tests/integration/m2/test_credential_rotation_repository.py backend/tests/integration/m2/test_oauth_refresh_coordinator.py backend/tests/integration/api/test_connections.py backend/tests/integration/google/test_oauth_flow.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_oauth_flow.py backend/tests/integration/microsoft/test_mail_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/operations/test_calendar_aad_0019_preflight.py -q
uv run --project backend pytest backend/tests/integration/operations/test_database_maintenance_gate.py backend/tests/integration/operations/test_postgres_backup_restore.py backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py -q
uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/retention/test_role_permissions.py backend/tests/unit/test_init_db_roles_script.py -q
bash scripts/test-legacy-backup-conversion.sh
bash scripts/test-deployment.sh
bash scripts/test-tooling.sh
python3 scripts/verify-m2-sensitive-output.py
git diff --check
~~~

It must also verify that the working tree has no `.env`, secret, dump, Playwright report, coverage, or generated release artifact staged for commit.
The release script test must fail if the fixed export is absent/late, a different or production-like DSN is accepted,
any child loses or overrides the exported value, or the explicit audit command is removed, reordered behind a
possible early success exit, or replaced with `just ci`; per-command prefixes and indirect coverage are not evidence.

- [ ] **Step 5: Run the complete automated release gate**

Run: `bash scripts/test-m2-release.sh`

Expected: PASS with fresh output from `just ci`, all crash cases, three independent fixed v1 digest vectors plus the
independent HKDF/HMAC `refresh_token_identity_v1` vector, and decrypt/validate/identity/started/lease/provider ordering.
The gate proves coordinator claim ordering, two-Worker single-call and response-after-lock-loss/CAS-miss zero-call
cases, versioned confirmed/unsatisfied/replacement actual-commit versus actual-rollback reconciliation through a fresh
session, exact automatic/recovery closure and replacement-only original-fence consumption, no guessed result or added
provider/code call, strict confirmed disposition/flag/equality and replacement old != new/changed=true parsing,
ACK-lost-then-later-valid-refresh/reauth closure, changed=false access-only/same-plaintext-re-encryption non-conflict,
and changed=true A→B→A/invalid-state `oauth_credential_state_conflict` zero-call blocking. It also proves revision-global plus connection-scoped leases across same/different basenames, provider-before-
started ordering, attempt/source/rollout/G/G/G/pre-post full-and-refresh digest/identity binding, unresolved cross-
basename zero-call blocking, and required confirmed persisted-expiry/disposition/`refresh_identity_changed`/
deadline-candidate/strict-timestamp
fields. Known-valid missing/same/different automatic responses all confirm; Google access-only preserves the refresh row
byte-for-byte with old == new/changed=false, while automatic different-token rotation is old != new/changed=true and
does not consume an old fence.

Explicit recovery covers `F=S`/`S>F`, atomic OAuthAttempt + recovery-started creation, and requested-capability
`authorizing` state. Denial, missing, same, and known provider failure converge only those capabilities to
`action_required` with stable error while preserving actual scopes/last-verified facts and the original fence; the
unsatisfied result has no credential post/expiry, stale `T` is a capability no-op, and a later new-attempt different-
token callback has target-generation-preserving old != new/changed=true F/S/T replacement proof. Stale-target/concurrent-reauthorization
rejection and full credential/scope/capability/event rollback remain covered. Google/Microsoft callbacks prove
mutually exclusive `code+state`/any-valid-`error+state`, pre-consumption shape rejection, unknown-error state
consumption, safe classification preserving `microsoft_admin_consent_required` and
`microsoft_reauthorization_required` with `oauth_authorization_failed` for denial/unknown names, raw-error redaction,
and replay rejection. Targetless fenced identity is
blocked before local persistence with no duplicate connection.

Shared full-row/generation CAS, repeated-recovery single-consumer arbitration, disconnected/old-plaintext-unavailable
blocking, post-consumption `action_required` reauthorization and generation changes without attempt revival,
fixed-root-key same-plaintext re-encryption preserving identity/changed=false plus changed=true A→B→A conflict,
result-union whole-group cutoff through the same strict parser with
user-isolated ordinary audit cleanup, ordinary completion-audit cutoff/privacy deletion while the completed call +
completion GUC pair remains authoritative, completed admission after audit deletion, malformed/missing/mismatched
completed catalog facts failing at the restore boundary, all fail-closed
provider/lease/CAS/rollback cases, Taskiq/
`TransientProviderError` zero-call delivery, closing-result-before-artifact resume under independent current readiness,
and no replay of original D0 all pass. The gate additionally covers the 0019 typed rollout guard
zero-bootstrap/affected-without-guard/Fake/final-commit paths, Task 16A's fixed schema-lifecycle exclusive lock across
Compose/just/integration/E2E/dedicated migration entrypoints, and proves Task 27C did not rewrite the frozen revision
or Alembic environment. Before any child command, the wrapper deterministically exports the exact Task13
`TEST_DATABASE_URL`; absent input selects it, an exact external value is accepted, every pytest/audit/shell child
inherits it, and any other or production-like DSN yields zero child calls. It directly executes the calendar-AAD
audit script, runs the four-entry real-Uvicorn OAuth query canary with persisted sanitized audit, and proves
ACK-lost/later-reauth current access rows can only tighten the original deadline. Generic backup tests prove
fixed global backup lock plus `${BACKUP_DIR}` flock, unique local/remote staging, 3600-second lock-held orphan
rechecks, shared schema lock across pre-revision/`pg_dump`/post-revision CAS, backup-vs-migration exclusion,
dump/checksum/versioned-manifest digest/size/revision/fingerprint/image/script binding, immutable image entrypoints
with no executable bind mount, `0600`, no-clobber/manifest-last local/remote
publication, and whole-group conflict/retention behavior. Generic owner restore succeeds for an ordinary 0019
manifest-bound backup without rollout artifacts; app-role restore is denied; any enabled write switch, running
general/owner consumer, missing production confirmation, manifest mismatch, lifecycle/target/authority conflict, or
pre-gate failure has `pg_restore_calls == 0`. Fixed low/high-bit digest/key vectors and management→target→schema order are proven;
reset after holder crash has zero drop/create/migration while active authority survives. Every owner entry shares
the same gate/call/completion catalog facts; live/crashed nonmatching calls have zero writes. The closed three-state
matrix accepts pristine all-absent + baseline ACL, active gate/call + absent completion + revoked ACL, and required
completed call/completion pair + baseline ACL only. Gate establishment validates/archives any prior completed pair,
RESETs completion and replaces it with the new active call in the same transaction, writes no AuditEvent, and then CONNECT is
revoked, existing writers terminate, and new writers are rejected. Backup/artifacts mount read-only; a separate
state-v4 projection follows catalog CAS. The psql consumer registers exact PID/backend-start/database/role/
`PGAPPNAME`; pg_restore is credential-free and emits SQL only after started CAS+projection fsync. Spawn-before-visible
and ready-before-feed are SQL-byte-zero, while missing trailer/mid-stream/controller or child failure rolls back.
Fingerprint/replay is blocked until the authority-bound backend exits; stale PID/backend-start and `pg_stat_activity`
alone are insufficient. commit+crash, rollback+crash, cross-host missing projection and inconsistent results reconcile
without blind replay. A cross-host `restore_backend_starting` invocation without local child/pipe identity must enter
`needs_attention` with zero pg_restore calls. The full phase graph/CAS/ordinals pass, including same-ordinal
call-start/pre/backend preservation and the sole legal `restore_not_applied` retry boundary with ordinal+1, fresh
call-start, exact-current pre re-freeze, old backend reset, starting-shape observed/completed fields, and no PID/start
inheritance. The holder performs grants and
app-role read-only verification before one completion-authority/GUC/fixed-outer-field ordinary-audit-ID/exact-
metadata/timeline/digest/gate-reset/minimal-CONNECT transaction; ACK loss is reconciled from the required completed
call + completion GUC, gate, and ACL
in a new session, with not-applied reopen
requiring `reopen_not_applied` and a new ordinal. The
dedicated legacy recipe uses a durable registry/labels/scavenger and creates a new backup rather than fabricating a
manifest. The sealed path adds owner-before-secret artifact/image/script validation with zero calls on mismatch,
then reuses the same restore/reconcile/verifier/reopen primitive under exact `sha256:`/no-pull rules.
Deployment/tooling contracts, sensitive-output scan, and `git diff --check` also pass.

- [ ] **Step 6: Pause for explicitly authorized provider-account E2E**

After the automated gate passes, stop before enabling real writes and request the user's explicit authorization plus out-of-band dedicated Google/Microsoft test-account configuration. Run the manual matrix only in a separate environment with the global/provider switches and exact account allowlist enabled. If authorization is not supplied, leave Task 30 incomplete and do not create a blank release-evidence document or claim M2 release completion.

If the target release environment has not yet applied 0019, the release operator must use the approved change
window to execute the non-rolling `docs/operations.md` sequence before enabling real writes: record the original
0018-compatible immutable image; stop new ingress and scheduling; drain all pre-0019
Caddy/API/Worker/Scheduler CalendarEvent readers/writers while PostgreSQL/Redis remain running; run and pass only
the no-argument `just calendar-aad-preflight-0019`, which first acquires the fixed revision-global PostgreSQL
session lease before artifact/provider access and then obtains each connection's shared coordinator lease, proves a
refresh token by exact-AAD decrypting it in controlled memory, validating non-empty UTF-8 and bounds, then computing
the fixed-root-key `refresh_token_identity_v1`; commits a content-free append-only
`oauth.refresh_started` before each first permitted provider call with canonical attempt UUID, source,
`rollout_digest_v1`, `fence_generation=F`, `pre_credential_snapshot_digest_v1`,
`pre_refresh_credential_snapshot_digest_v1`, old identity and key version; proactively refreshes each newly fenced
distinct connection once; validates returned scopes; atomically commits complete old-snapshot CAS plus new
credential/expiry and an attempt-matching `oauth.refresh_confirmed` with G/G/G, required full/refresh post-digests,
old/new identity/version, actual persisted expiry, missing/same/different disposition, `refresh_identity_changed`, its
derived deadline candidate, and strict timestamp; missing/same is old == new/changed=false and different is old !=
new/changed=true; and creates the original artifact deadline from the minimum current persisted access expiry minus
900 seconds, or produces the exact zero/no-deadline state when no pair is affected. Historical confirmed deadline
candidates validate only their result schemas. After ACK-loss closure/current readiness and at every later guard,
reload every affected connection's current access expiry and use
`effective_deadline = min(original_artifact_deadline, current_deadline)`; shorter expiry tightens immediately and
longer expiry never extends the window. Known-valid missing/same/different automatic responses all confirmed; missing/same preserves the refresh
row with changed=false, while different updates it with changed=true without consuming any older fence. An unresolved fence blocks every basename and all
Taskiq/`TransientProviderError` re-entry must make zero provider calls. If a result commit ACK is lost, a fresh
database session must reconcile by user/connection/attempt using the versioned confirmed/unsatisfied/replacement union:
an actual commit permanently closes its allowed automatic/recovery attempt without adding calls, while an actual
rollback has no result and leaves that started unresolved; unsatisfied does not consume original fence, replacement
does. The parser rejects confirmed disposition/flag/equality mismatches and replacement without old != new/changed=true.
Closure is independent from current readiness: a later valid refresh/reauth uses current facts; changed=false after
access-only refresh or same-plaintext re-encryption is normal. changed=true followed by current identity equal to old
is A→B→A; that rollback, missing rows, invalid AEAD/ownership, or inconsistent state returns
`oauth_credential_state_conflict` and blocks rollout at zero calls. No
guessed result is permitted. Recovery requires a user-created,
connection-bound progressive OAuthAttempt whose start transaction appends
`oauth.refresh_recovery_authorization_started` and sets requested capabilities to `authorizing`; callback exchanges
that code once without a second automatic started. Denial, missing/same or safely known provider/network failure at
current `T` appends the versioned unsatisfied result and atomically changes only those requested capabilities to
`action_required` with a stable error while preserving actual scopes/last-verified facts and without credential
post/expiry fields; stale `T` is a capability
no-op plus safe audit. The original fence remains and the user may create another OAuthAttempt. Only a different
non-empty refresh token may atomically commit credential/scopes/capabilities plus
matching `oauth.refresh_credential_replaced` with complete old != new, `refresh_identity_changed=true`, F/S/T
`source="progressive_recovery"` proof and strict
timestamps. Targetless fenced-identity callback must fail before local credential/scope/capability persistence;
Google/Microsoft callbacks must accept only `code+state` or bounded non-empty `error+state`; missing/ambiguous/neither/
malformed shape rejects before consumption, any valid error including an unknown name consumes state once and returns
after target-T unsatisfied/no-op using the safe classification matrix: denial and unknown names are
`oauth_authorization_failed`, Microsoft consent evidence is `microsoft_admin_consent_required`, and ordinary
`interaction_required` is `microsoft_reauthorization_required`. Raw error is not recorded or persisted as an error
code, and replay rejects. Normal Google/Microsoft
mail/calendar automatic refresh cannot consume the fence. Once valid, replacement proof
permanently closes both matching recovery and old automatic attempts while current readiness is checked independently;
changed=false current identity equal to old == new after access-only refresh or same-plaintext re-encryption is normal
even when physical digest differs. changed=true current identity equal to old is A→B→A; that rollback, missing rows,
invalid AEAD/ownership, or inconsistent state is `oauth_credential_state_conflict`, but never reopens/calls the old
attempt. Disconnect/reconnect, a new connection mapping, missing/same refresh token, or unavailable
old plaintext cannot produce consumption. If the affected scope remains, use only an existing explicitly user-
authorized data-disposition flow; otherwise stop and request a newly approved design.
Closing result surviving a pre-artifact crash resumes from persisted post token/expiry when exact, or later valid
current token/expiry after readiness, without another old-attempt refresh; the current expiry is then used only to
tighten the original window. Then back up and
capture the pre-migration audit artifact; migrate only through `just calendar-aad-migrate-0019` and capture the
post-migration artifact; make the v2-only immutable image available while all general services remain stopped; run
only the ordinal-aware `just calendar-aad-resync-0019`; capture and pass the post-resync artifact under the same
typed rollout guard; then start API/Worker/Scheduler/Caddy, run health checks, and verify each affected exact pair.
Neither `scripts/test-m2-release.sh` nor Task 16A may automate these production actions. Do not continue to the
provider matrix while preflight is locked, loses its lease, has an unresolved/needs-attention refresh fence,
credential CAS failure, unreconciled commit-result-unknown, confirmed rollback or any failed connection/pair, a non-empty effective deadline has passed, zero-state
revalidation fails, any affected scope lacks recovered cursor/freshness, retains
`calendar_event_resync_required`, has synchronized non-empty fields that are not v2, or while the post-resync audit
has not passed before general service startup.

If 0019 has committed but marker recovery/post-resync cannot finish before the current effective deadline, the production matrix
must remain paused. While the same maintenance window is still sealed and no general service/business write has
occurred, use only that window's active-refresh-after/pre-migration encrypted whole-database backup with the
guarded production `just calendar-aad-restore-0018` confirmations through the dedicated PostgreSQL-only sealed
owner-role operations service. Generic `just restore` is not valid for this exception. Before owner
Secret/connection/`pg_restore`, the host must independently validate basename, dump/checksum/manifest,
preflight/`pre-migration` artifacts, image binding, and image-internal executable digests; mismatch or executable
bind mount must have zero owner and `pg_restore` calls. The
service must use only the internally injected matching `sha256:` image with no tag/build/pull and fixed
`--pull never`; its entrypoint repeats the artifact/image/script check before reading the owner Secret. It then
claims `kind=sealed_0018` through the same management/target/exclusive-schema locks, database gate/call/completion
facts, read-only artifact plus separate state-v4 projection, psql backend registration, deferred-guard
credential-free pg_restore SQL stream, and authority-bound backend-exit/ordinal reconcile executor as generic. The holder
session runs grants and the sealed revision/artifact verifier under
`SET ROLE ai_employee_app` + `BEGIN READ ONLY`/SQLSTATE `25006` before the final completion-GUC/fixed-outer-field-
exact-metadata-audit/gate-reset/minimal-CONNECT transaction; ACK unknown is resolved by new-session expected
completed call + completion GUC zero-slot binding, gate, and ACL inspection, with audit absence after
retention/privacy remaining valid and definite
rollback entering `reopen_not_applied`. Start only the recorded
original 0018-compatible image after that tuple and health pass. Record this as an aborted 0019 window, not a
successful rollout. Alembic downgrade, direct SQL,
marker waiver, temporary
OAuth-only service, or continuing provider E2E after restore verification failure is forbidden.

- [ ] **Step 7: Write the completed release-evidence record**

Create `docs/releases/2026-08-06-m2-release-evidence.md` only after the manual matrix is complete. Record actual date, operator, commit, environment, locally hashed dedicated-account identifiers, exact enabled switches, Google new/reply/reply-all/create/update/restore results, Microsoft parity results, alternate Microsoft account-type contract evidence, approval/ToolExecution audit IDs, scope review, backup/restore, crash drill, sensitive-output scan, and the final release decision.

The same record must contain the actual non-rolling 0019 rollout evidence: ingress/scheduler-stop and
Caddy/API/Worker/Scheduler drain timestamps; the recorded original 0018-compatible image; proof no legacy reader,
upsert, general service, or business write remained; revision-global and per-connection lease results; the accepted
`rollout_digest_v1`; the three independent credential/rollout vectors and fixed `refresh_token_identity_v1` vector;
hashed affected connection/pair counts; exact decrypt → validate → identity → committed started/lease → provider
ordering; one proactive refresh per connection; returned-scope validation; complete generation/access/refresh
snapshot CAS; and content-free confirmed/unsatisfied/replacement actual-commit/actual-rollback evidence without
fabricated calls or results.

For deadlines, record the original artifact deadline, every current access-credential expiry recheck in approved
content-free aggregate form, every computed current deadline, and the effective deadline used at each backup, audit,
migration, resync, restore-eligibility, and post-resync guard. Include the ACK-lost race where a later shorter expiry
tightened the window and a longer expiry did not extend it; historical confirmed deadline candidates are recorded
only as schema-valid fields. The post-resync artifact commit must precede both the effective deadline and general
service startup.

Record backup/preflight-state/migration identifiers and checksums without secrets; the host-resolved image ID and
match at every one-off; the deployed v2-only image digest; every recovery invocation and ordinal reuse/allocation;
all start/pre-commit guard results; and the preflight, `pre-migration`, `post-migration`, and `post-resync` artifact
checksums. Record exact affected pairs only through locally hashed connection/calendar identifiers, their
cursor/freshness/marker transitions, v2 field evidence, and immutable prior/new TaskRun facts. If the set was empty,
record the evidenced zero/no-deadline branch. Also record the normal 0019-compatible rollback-floor image.
Credential bytes, per-attempt token timestamps beyond approved aggregates, identity fingerprints, raw provider
errors, and correlatable credential `updated_at` values are forbidden.

The release record must additionally prove API deps, mail Worker, calendar Worker and the preflight CLI constructed
`AeadCipher` plus `refresh_token_identity_v1` from the same fixed `APP_MASTER_KEY_FILE` bytes and key version. M2
must not claim a mutable root, multi-key lookup coverage, or a second identity Secret.

It must also record all four Uvicorn launch entries and the real subprocess callback canary. Synthetic `code`,
`state`, and `error_description` must have zero matches in stdout, stderr, and application JSON logs, while the
sanitized callback-error audit and one-time state consumption are present in PostgreSQL. Store only canary IDs and
pass/fail, never the synthetic query values.

For each affected connection, the record must identify content-free `oauth.refresh_started` and
`oauth.refresh_confirmed` result codes; their schema, source/started source, canonical attempt UUID,
`rollout_digest_v1`, hashed connection, G/G/G, pre/post full/refresh digest-field presence/match without digest values,
old/new identity-field presence/match with the fixed key versions but no fingerprint values, persisted-expiry/
deadline-candidate field presence/match while emitting only the approved global earliest expiry/deadline,
missing/same/different disposition, `refresh_identity_changed`, the required disposition/flag/equality relation, and
strict `created_at` ordering. Evidence both leases and started committed
before the provider call, and credential CAS plus confirmed committed in one transaction. Record automatic access-only
confirmed as old == new/changed=false and different-token confirmed as old != new/changed=true; the latter must show no
old-fence consumption. Record every unresolved
fence and demonstrate all later same/different-basename and Taskiq/`TransientProviderError` automatic entries made zero
provider calls.

The record must separately cover injected actual commit and actual rollback for every
`OAuthRefreshResultV1 = ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1` member. A fresh-session
user/connection/attempt read-only reconcile must prove that confirmed/replacement are the only allowed closers for a
matching automatic attempt, unsatisfied/replacement are the only allowed closers for a matching recovery attempt, and
replacement closes both while only it consumes the original automatic fence. The same parser used by retention must
reject confirmed disposition/flag/equality mismatches and replacement without old != new/changed=true. Every actual
commit must preserve the
already-observed provider/code call count without adding a call: confirmed/replacement retain one total provider call,
an error-callback unsatisfied may retain zero, and an unsatisfied after exchange may retain one;
every actual rollback must have no matching result, leave the corresponding started unresolved/`needs_attention`, and
make every later provider/code call zero. Loss of the original lease/session must not prevent either check, and no
guessed result may be appended. Closure evidence must then be followed by independent current-readiness evidence:
exact confirmed/replacement post-state may resume directly; a later valid refresh/reauth keeps the historical attempt
closed and proceeds from current facts. changed=false after access-only refresh or same-plaintext re-encryption is
non-conflicting even if the physical snapshot changed. changed=true followed by current identity equal to old is
A→B→A; that rollback, missing credential rows, invalid AEAD/ownership, or inconsistent current state returns
`oauth_credential_state_conflict`, blocks rollout, and keeps `provider_calls == 0`. Unsatisfied
has no credential post/expiry proof and is checked only against current capability facts.

For each explicit recovery,
record `oauth.refresh_recovery_authorization_started` schema/source, OAuthAttempt and event IDs, connection digest,
original attempt/started source, F/S/T, both frozen pre-digest field presence/match, old identity-field match/key
version, stable result code, and strict timestamp after the original automatic started, without recording any
digest or fingerprint value. For every unsatisfied result, record
`result_schema_version="oauth_refresh_recovery_unsatisfied.v1"`, its matching recovery event/OAuthAttempt IDs,
original attempt/started source/connection
digest, F/S/T, both frozen pre-digest field presence/match, old identity-field match/key version, canonical requested
capabilities, stable error/result codes, `capability_transition`, and strict timestamp after the recovery-started
event. Demonstrate denial, missing, same and safely known provider failure at current target `T` each atomically
changing only requested capabilities from `authorizing` to `action_required` while preserving actual scopes and
last-verified facts; prove the unsatisfied schema contains no credential post snapshot/digest, new identity,
persisted-expiry or deadline field. Demonstrate stale `T` as `stale_target_noop` without overwriting newer authorization
state. Each unsatisfied attempt closes only itself and leaves the original fence, followed by a new OAuthAttempt whose
different-token callback atomically commits
`oauth.refresh_credential_replaced` with `source="progressive_recovery"`, original
attempt/started source/connection digest, recovery OAuthAttempt/event IDs, complete F/S/T with
`pre_generation=post_generation=T`, original pre-digest and required post-digest field presence/match without digest
values, old/new identity-field match and key versions without fingerprint values, persisted-expiry field match
without a per-attempt timestamp, `refresh_identity_changed=true`, old != new, stable result code, and timestamps
strictly later than both started events while
generation remains `T`. Record the
`compare_digest` decision class, complete row/generation/identity CAS result, single-consumer arbitration, two-Worker
one-call proof, response-after-lock-loss/CAS-miss/unknown/Taskiq zero-call evidence, commit-ACK-loss reconcile evidence,
and injected atomic rollback
without token or plaintext hash. Targetless fenced-identity callback must show local credential/scope/capability
writes and duplicate-connection creation were zero. Google and Microsoft API evidence must show mutually exclusive
`code+state`/bounded non-empty `error+state`; missing state, code/error ambiguity, neither field, and malformed error
must reject before state consumption. Any valid error, including an unknown name and `access_denied`, must consume state
once and perform target-`T` unsatisfied/no-op convergence. Evidence must assert the safe classification matrix rather
than one Problem: denial and unknown names use `oauth_authorization_failed`, Microsoft consent evidence uses
`microsoft_admin_consent_required`, and ordinary `interaction_required` uses
`microsoft_reauthorization_required`. Keep raw error/description/codes out of persistence, stable error-code storage,
logs, and Trace; replay of the consumed state with the same or a different unknown error
must reject. No-token/empty/same-plaintext/access-only/re-encryption, stale CAS, unknown-result, disconnected/old-
plaintext-unavailable, and unsatisfied-recovery paths must show no original-fence consumption and continued blocking;
disconnect/reconnect or new-connection mapping must never be presented as recovery evidence. Automated evidence must
also show every legal result closure surviving later capability `action_required` plus reauthorization and later
generation/scope/credential changes; replacement consumption remains valid. changed=false same-plaintext
re-encryption is normal, while changed=true current identity returning to old is A→B→A and
`oauth_credential_state_conflict` without reopening or recalling the old attempt. Missing rows, invalid AEAD/ownership,
or other inconsistent state remain conflicts regardless of flag. Prove no replay of the original D0
preflight refresh. Include cutoff evidence that an unresolved started older than the
ordinary audit retention period remains, still blocks with zero provider calls, and does not prevent another user's
or ordinary audit cleanup. Also prove automatic-success, unsuccessful-recovery and successful-recovery groups are
deleted through the same strict result-union parser only when every event is older than cutoff, without comparing later
credential lineage, so an old started plus newer result remains. If a closing-result-before-artifact/reconcile crash occurred,
record that exact post-state or later-valid current readiness resumed without another old-attempt refresh or code
exchange. Include failure disposition for lock loss, network unknown, `invalid_grant`, malformed response, scope
shrink, CAS failure, confirmed/unsatisfied/replacement rollback, and commit-result-unknown reconciliation, without
recording token, raw scope, provider response, raw `calendar_id`, or correlatable credential timestamps.

Include restore-completion retention/privacy evidence as well: an old
`database.restore.completed`/`ai_employee.database_restore_completed.v1` row must be removed by the ordinary 365-day
cutoff while the exact completed `restore-call:v4` and
`ai_employee.restore_completion=restore_completion:v1:<digest>` remain unchanged, and a fresh completed
admission/ACK check must still succeed from the zero-slot-bound pair + gate absence + baseline ACL. All-data deletion must remove that
user's completion audit without a special exemption, preserve the sole inactive anonymized `users` row, and a later
restore completion must attach its new fixed-outer-field audit to that row. AuditEvent's ordinary BigInteger ID,
existence/count, event/schema/metadata, and digest are not authority. Malformed completion GUC or missing/mismatched
completed call fails closed at restore admission/ACK; retention does not parse catalog facts or preserve completion
audits.

The normal release record must first include a generic disaster-restore drill for a supported backup revision,
including an ordinary 0019 backup: versioned manifest schema, safe basename, UTC creation time, PostgreSQL/Alembic
revision, encrypted dump digest/size, checksum filename, immutable backend image/content-free metadata, expected-post
`restore_fingerprint_v1`, the allowlisted image-internal executable digest map, `0600`, and
dump/checksum/manifest cross-binding. Prove the release image was built from repository-root context with the actual
`backend/Dockerfile` COPYs for every operations entrypoint and that no service overrides an executable with a bind
mount. Record the fixed database backup lock plus `${BACKUP_DIR}` flock, unique local/remote staging, manifest-last
no-clobber publication, 3600-second rechecked orphan cleanup, and whole-group retention/conflict behavior. Record
shared `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` across pre-revision/`pg_dump`/post-revision CAS, the matching migration
exclusive lock, a backup-vs-migration race, and no publication on lock loss or revision change.

Record all three write switches off, Caddy/API/Worker/Scheduler/migration/role-bootstrap/general plus owner consumers
stopped, the post-state-check production confirmation, and zero writer before target-gate establishment. Then record
the exact system-identifier/database input bytes, 1–63-byte database-name boundary, fixed low/high-bit digest and
lifecycle/target lock-key synthetic vectors, and management→target→schema lock order. Prove restore holds the management lock from `postgres`
through terminal reconcile/reopen, while `db-reset` holds it through check→drop→create→migration; after holder crash,
active/`needs_attention` catalog authority yields zero drop/create/migration. Record the nonblocking target holder,
exact database-wide `ai_employee.maintenance_gate`, `ai_employee.restore_call_authority`, and
`ai_employee.restore_completion`, the closed pristine/active/completed admission snapshots, CONNECT revocation,
existing-session termination, new-connection denial, and concurrent-writer/running-service zero restore. Do not
require a pre-restore gate audit: `pg_restore --clean` may replace `audit_events`; record catalog snapshots and
state-v4 checksums instead.

Backup/artifact mounts must be read-only; record the separate writable `RESTORE_STATE_DIR` mode-`0600` state-v4
projection and a second-host reconstruction with no local file. The database authority must contain exact attempt/
kind/target/source/manifest, previous completion digest, pre/expected/observed facts, `call_ordinal`, independent
`reopen_ordinal`, phase, gate/call/completed timeline, completion digest, and last psql PID/backend-start. Record exact
ASCII lengths/delimiter counts, duplicate/malformed parser rejection, every legal frozen phase edge, and rejection of
jumps/self-transitions/stale CAS. Record same-ordinal preservation of call-start/pre/backend/observed facts and the
sole controlled real-call ordinal boundary: only after exact-pre proof may the holder CAS to starting with ordinal+1,
a new database call-start, exact-current pre re-freeze, backend PID/start reset and observed/completed starting shape;
the previous ordinal's backend facts cannot carry forward, while direct exact-post remains the ordinal-0 shape. Record
`restore_backend_starting` before spawn, exact PID/backend-start/database/role/`PGAPPNAME` registration before ready,
and started CAS plus projection fsync before the first SQL byte or pg_restore call. Prove only the psql consumer
receives the bootstrap owner credential, the pg_restore generator receives no `PG*`/DSN/Secret value, and the
controller-owned pipe/deferred completion guard rolls back on controller crash, missing trailer, generator/consumer
failure, or mid-stream EOF. Fingerprint/not-applied/replay remains blocked until the authority-bound backend exits;
stale PID/backend-start or `pg_stat_activity` alone is insufficient. Record that another host with no provable local
child/pipe identity transitions starting to `needs_attention`, makes zero pg_restore calls, and cannot allocate an
ordinal; only the original still-live controller may take starting→ready. Record spawn-before-visible and
ready-before-feed SQL-byte-zero, exact-post/exact-pre/inconsistent, grant rollback staying `restore_succeeded`,
verifier failure staying `grants_succeeded`, and explicit new ordinal retry only from legal `restore_not_applied`.

Record that a second restore of an older backup verifies and fsyncs an immutable archive of the prior completed pair,
then in the same owner transaction replaces the old completed call with the new active call, resets the prior
completion GUC, establishes its gate, and revokes CONNECT. Record holder-session grants and app-role read-only verification
before one final transaction requires exactly one active-or-inactive-anonymized administrator row, CASes authority to
completed, appends one ordinary BigInteger-ID audit with fixed `user_id`/NULL task/event/system actor/database-time
outer fields, safe gate/call timeline, expected/observed facts, and exact metadata/digest, sets
`ai_employee.restore_completion=restore_completion:v1:<digest>`, retains the mutually bound completed call, resets
gate, restores only app/retention CONNECT, and leaves PUBLIC revoked. ACK-loss evidence from a new owner session must
read call + completion and distinguish completed from definite absent-GUC active not-applied using the pair + gate +
ACL; audit retention/privacy deletion does not invalidate completed, but a missing catalog call does.
`reopen_committing → reopen_not_applied`
retry requires a new `reopen_ordinal`; a prior completion value in an active attempt or other mixed tuple enters
`needs_attention`. Record a host crash immediately after committed reopen and prove no replay. The local projection
must never be presented as database authority. Explicitly prove no 0019 rollout artifact was required.

Also record production rejection of pre-manifest input and the exact non-production
`just restore-legacy-to-isolated file output_basename` conversion: checksum/name/conflict validation, unique Compose
project/internal network/ephemeral volume/temporary `0600` Secret, actual revision/health read, ordinary locked new
backup publication, success/failure cleanup, zero workspace/production database access, and no fabricated manifest
or sensitive output. Include the fsynced pre-resource durable registry, exact cross-resource labels, fixed
3600-second grace, per-attempt nonblocking lock, inactive-container recheck, and SIGKILL/Docker-daemon/host-crash
scavenger evidence while proving another live or uncertain attempt was preserved.

If the window instead used sealed restore, the same record must contain the exact failure/effective-deadline guard, proof that no
general service or business write occurred after backup, encrypted backup/checksum/manifest identity, production restore
confirmations for `just calendar-aad-restore-0018`, dedicated sealed restore service/image identity, proof it depended only on PostgreSQL and used
the bootstrap owner credential solely for the fixed-target psql consumer while credential-free pg_restore only
generated SQL through the controller-owned guarded stream, proof the host validated basename/dump/checksum/manifest/preflight/
`pre-migration`/image binding before any owner action, and zero owner/`pg_restore` calls for every moved-tag,
missing/wrong-image, or artifact-mismatch case. Record the exact internally injected `sha256:` restore image, no
tag/build/pull and `--pull never` facts, the container pre-Secret guard, fixed psql consumer and credential-free
pg_restore generator flags/results, read-only backup/artifact mounts plus separate state volume, and the matching
`kind=sealed_0018` gate/call-authority/completion-GUC binding, with grants on the existing holder session without early
CONNECT reopen. The sealed verifier evidence must show that same owner session executing
`SET ROLE ai_employee_app`, `BEGIN READ ONLY`, current/session-user assertions, and same-connection DML rejection
with SQLSTATE `25006` before the shared final fixed-outer-field completion-audit/gate-reset/minimal-CONNECT
transaction. Prove there is no standalone post-reopen app-only verifier and no success-affecting verification after
reopen; ACK unknown uses the same new-session completed call + expected completion GUC + gate +
ACL/`reopen_not_applied`
reconciliation as generic, and audit absence after retention/privacy remains completed. Also record restored revision 0018, deterministic
restored-audit digest comparison, original 0018 image digest and health result, and an explicit
`0019 rollout aborted` decision;
it must not present the v2 rollout or provider matrix
as complete. Every row must contain evidence or an explicit failed result with disposition; the file must contain
no blank field or future-action marker, and artifacts must contain no DSN, plaintext content, raw provider calendar
ID, token, scope string, or credential material.

- [ ] **Step 8: Commit**

~~~bash
git add backend/tests/integration/faults/test_m2_write_recovery.py backend/tests/integration/faults/conftest.py backend/tests/integration/db/test_migrations.py backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py frontend/e2e/m2-actions.spec.ts backend/src/ai_employee/infrastructure/testing/scenarios.py backend/src/ai_employee/infrastructure/testing/test_support.py backend/src/ai_employee/api/routers/test_support.py scripts/test-m2-release.sh scripts/test-tooling.sh scripts/verify-m2-sensitive-output.py docs/releases/2026-08-06-m2-release-evidence.md docs/acceptance-checklist.md README.md
git commit -m "test: freeze M2 release evidence"
~~~

## Spec Coverage Review

- Scope, single-operation control, exclusions, multi-account binding, and one-release Google/Microsoft parity: Tasks 1–3, 8–13, 21–24, and 30.
- Typed commands, canonical hashes, encrypted storage, exact approval, 10-minute decision window, five-minute claim window, invalidation, and legacy fake-write compatibility: Tasks 2–6 and 18–19.
- Draft generation/editing, reply/reply-all rules, recipient limits, model minimization, empty fallback, and no provider draft: Tasks 2, 11, 14–15, 21, and 23.
- Calendar directory, working hours, buffer, 15-minute grid, 14-day horizon, three candidates, completeness, create/update/restore, ETag, attendees, and notification policy: Tasks 4, 12–13, 16–17, 22, and 24.
- Calendar proposal trust hardening, including time-valid Worker leases, independent freshness observation, fixed-query minimal projections, transaction-free merged-interval computation, expected-version CAS, exact restore input, explicit-confirmation invariants, and operation-specific readiness: Task 16A.
- CalendarEvent field AAD v2, forward-only 0019 rotation, local recoverability, cross-calendar ciphertext
  isolation, fail-closed reads, and the frozen mutation/final-commit typed migration guard with fresh/strict-zero
  ordinary `upgrade head`, plus the outer schema-lifecycle exclusive lock shared by every online migration entry:
  Task 16A. Revision-global lease, artifact-backed injection without revision rewrites,
  exact-scope probes, current-access-row effective-deadline tightening, ordinal recovery, and post-resync-before-
  start: Task 27C. Generic backup global/local serialization, shared schema lock plus revision CAS, manifest/group
  publication, fixed low/high-bit target vectors and management lifecycle/reset boundary, production
  gate/call/completion catalog facts with state-v4 psql backend registration/deferred-guard credential-free
  pg_restore stream/backend-exit/ordinal/completion-GUC disaster restore, exact non-production isolated legacy conversion, independent
  sealed 0018 restore, three-phase audit, and explicit release audit wrapper: Task 27D.
  Four-column retention and operator-facing integration: Task 27E. Final
  migration/deadline/restore/audit evidence: Task 30.
- OAuth refresh trust hardening is split by ownership: Task 27A owns the fixed-root identity, shared automatic
  coordinator, full-row/generation CAS, strict confirmed/result parser, zero-call replay behavior, and four-entry
  real-Uvicorn access-log canary. Task 27B owns connection-bound F/S/T recovery, unsatisfied/replacement closure,
  callback error consumption/classification, and targetless pre-save blocking. Task 27C consumes both for 0019
  preflight and current-readiness/deadline guards. Task 27E owns whole-group retention. Task 30 records the
  corresponding content-free result-reconcile/recovery/fence/CAS/log/deadline evidence.
- Progressive Google/Microsoft OAuth, personal/work accounts, scope dependencies, capability shutdown, disconnect, and revoke: Tasks 4, 8–10, and 25.
- Idempotent claim, safe retry, lease-valid completion, unknown-result reconciliation, manual resolution,
  compensation, Redis/checkpoint recovery, and crash points: Tasks 3, 16A, 18–25, Tasks 27A–27B, and Task 30.
- API, SSE, action center, structured previews, needs-attention, accessibility, responsive layout, and server-authoritative recovery: Tasks 15, 17, 26, 28, and 29.
- Security, no-store, access-log closure, redaction, retention, source deletion, all-data barrier, metrics, alerts,
  deployment switches, dual restore, rollback, incident response, and release evidence: Tasks 1, 5–6, 25–26,
  Tasks 27A–27E, and Task 30.

## Execution Order and Checkpoints

1. Tasks 1–6 establish the approved scope, pure contracts, migrations, and encrypted persistence. Checkpoint: empty-database migration and AAD/user-isolation tests pass before any provider expansion.
2. Tasks 7–13 generalize reads and add Microsoft/Google parity. Checkpoint: both providers pass OAuth plus incremental mail/calendar contract tests with real writes still disabled.
3. Tasks 14–20 add editable proposals and the provider-independent trusted execution/reconciliation core. Within this stage, execute Task 14 → Task 16 → Task 16A → Task 18 → Task 15 → Task 17 → Tasks 19–20. Task 16A must close calendar lease, freshness, bounded-query, transaction, confirmation, restore-input, and CalendarEvent AAD v2/0019 trust gaps before any proposal version can be frozen. Task 18 must precede the two REST tasks because their `/submit` routes can only return an honest, durable `202` after the atomic encrypted approval-submission use cases exist; creating an unhandled placeholder task is forbidden. Task numbering remains grouped by domain and does not imply execution order inside this stage. Checkpoint: Fake adapters prove one claim, no blind retry, and durable needs-attention/manual resolution.
4. Tasks 21–24 add one high-risk provider write path per task and commit. Checkpoint: each adapter independently passes success, rejection, timeout, unknown-result, reconciliation, and duplicate-delivery contracts before starting the next adapter.
5. Tasks 25–26 and 27A–27E close revocation, API/SSE, observability, OAuth recovery, 0019 operations, privacy, retention, and
   deployment gaps. Execute the split work strictly as Task 25 → Task 26 → Task 27A → Task 27B → Task 27C →
   Task 27D → Task 27E.

   Task 16A is the sole owner of the final 0019 revision, public `AeadCipher.key_version`, typed migration-guard
   port, mutation-phase invocation, zero-bootstrap behavior, and `on_version_apply` final guard. Task 27C treats
   those files as read-only and only injects its concrete artifact-backed guard.

   Checkpoint 5 requires fresh evidence for: four Uvicorn entries with access logs disabled and a real OAuth-query
   subprocess canary whose sanitized audit still persists; fixed digest/HKDF vectors; shared automatic coordinator
   one-call/zero-replay behavior; strict confirmed/unsatisfied/replacement parsing and ACK-loss actual commit/
   rollback reconcile; F/S/T progressive recovery and callback classification; current-readiness separation;
   current-access-row effective-deadline tightening where shorter expiry tightens and longer expiry does not extend;
   revision-global plus connection leases; exact-pair probes and ordinal recovery; Task 16A fresh/zero/missing/Fake/
   final migration-guard paths plus outer schema-lifecycle lock; manifest-bound generic backup publication with
   management/global/local locks, revision CAS, group retention/remote/orphan handling; lifecycle-wrapped reset with
   crashed-holder zero drop/create; gate/call/completion catalog facts plus read-only artifacts/separate state-v4
   projection, psql backend registration/deferred-guard pg_restore SQL stream/backend-exit/ordinal owner disaster
   restore of ordinary 0019 backups without rollout artifacts,
   same-owner-session role verifier and unknown-outcome/reopen-not-applied resume; dedicated
   isolated legacy conversion/cleanup;
   independent sealed `calendar-aad-restore-0018` owner-before-secret/exact-image/read-only verification; explicit
   release-script execution of `scripts/test-calendar-aad-0019-audit.sh`; whole-group fence retention; privacy
   deletion barrier; and post-resync-before-start. No Task 27A–27E commit may combine another subtask's file list.
6. Tasks 28–29 deliver the frontend projection and focused editors. Checkpoint: unit tests, strict type checking, lint, production build, keyboard/focus, and mobile behavior pass.
7. Task 30 runs the full automated gate from one exact exported Task13 `TEST_DATABASE_URL`, including the direct
   calendar-AAD audit call, migration guard matrix, access-log subprocess canary, current-expiry deadline tightening,
   management/schema/backup lock and revision-CAS races, reset lifecycle bypass regression, manifest-bound
   gate/call/completion state-v4 psql backend registration/deferred-guard SQL stream/backend-exit/ordinal/
   completion-GUC generic restore, isolated legacy cleanup, and
   sealed restore separation; then it
   pauses for explicit provider-account authorization, performs the dedicated Google/Microsoft matrix, and writes a
   fully evidenced final release record. Without that authorization, Task 30 remains incomplete.
