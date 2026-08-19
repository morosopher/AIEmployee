"""验证本地版本只能冻结为一个加密、精确绑定的可信审批。"""

import importlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType
from uuid import UUID

import pytest

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.connections import CapabilityStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import TaskStatus

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
GOOGLE_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")
DRAFT_ID = UUID("00000000-0000-0000-0000-000000000301")
TASK_ID = UUID("00000000-0000-0000-0000-000000000401")
STEP_ID = UUID("00000000-0000-0000-0000-000000000402")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000403")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000601")
PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000000302")
BEFORE_SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000502")
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _trusted_action_modules() -> tuple[ModuleType, ModuleType]:
    """在测试执行期加载计划要求的新端口与用例，避免缺文件变成收集错误。"""
    ports = importlib.import_module("ai_employee.application.ports.trusted_actions")
    use_cases = importlib.import_module("ai_employee.application.use_cases.trusted_actions")
    return ports, use_cases


class _FakeCommandCipher:
    """记录待加密命令，并返回形状合法的合成 AEAD 三元组。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def encrypt_json(self, payload: dict[str, object], **_: object) -> EncryptedValue:
        """复制标准 JSON 命令，证明用例只冻结一次完整载荷。"""
        self.payloads.append(dict(payload))
        return EncryptedValue(ciphertext=b"synthetic-ciphertext", nonce=b"n" * 12, key_version=1)


class _FakeWritePolicy:
    """为合成 Google 连接打开审批前写入门禁。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """仅允许测试声明的 Google provider。"""
        return provider == "google"

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """只允许固定合成账户键，避免测试掩盖精确账户检查。"""
        return provider_identity_key == "google::synthetic-account"


class _FakeTransaction:
    """提供可独立控制的提交事务状态，并记录原子创建调用。"""

    def __init__(
        self,
        *,
        mail: object | None = None,
        calendar: object | None = None,
        consumed: bool = False,
        existing: object | None = None,
    ) -> None:
        self.mail = mail
        self.calendar = calendar
        self.consumed = consumed
        self.existing = existing
        self.created: list[object] = []

    async def find_existing_submission(self, **_: object) -> object | None:
        """返回同用户幂等键已有任务，模拟数据库先查重顺序。"""
        return self.existing

    async def lock_mail_draft(self, **_: object) -> object | None:
        """返回锁定草稿版本。"""
        return self.mail

    async def lock_calendar_proposal(self, **_: object) -> object | None:
        """返回锁定日程提案版本。"""
        return self.calendar

    async def proposal_version_is_consumed(self, **_: object) -> bool:
        """表示当前版本是否已被任一 pending 或 terminal 审批引用。"""
        return self.consumed

    async def create_submission(self, submission: object) -> None:
        """记录一次应由真实仓储原子写入的提交事实。"""
        self.created.append(submission)


class _FakePreflight:
    """记录纯审批预检收到的领域命令。"""

    def __init__(self, ports: ModuleType, *, provider: str = "google") -> None:
        self._ports = ports
        self.provider = provider
        self.commands: list[object] = []

    def validate_for_approval(self, command: object) -> object:
        """不访问网络，只记录命令并返回无警告结果。"""
        self.commands.append(command)
        return self._ports.ApprovalPreflightResult()


class _FakePreflights:
    """按精确 provider/action 返回固定预检，或模拟 fail-closed 缺失。"""

    def __init__(self, preflight: _FakePreflight | None) -> None:
        self.preflight = preflight
        self.requested: list[tuple[str, str]] = []

    def trusted_action_preflight(self, *, provider: str, action: str) -> object:
        """记录精确选择，并在未组装时抛稳定不可用错误。"""
        self.requested.append((provider, action))
        if self.preflight is None:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="provider action is unavailable",
            )
        return self.preflight


def _mail_snapshot(ports: ModuleType, **overrides: object) -> object:
    """构造一个完整、可提交的新邮件版本，再按测试覆盖指定字段。"""
    values: dict[str, object] = {
        "draft_id": DRAFT_ID,
        "connection_id": GOOGLE_CONNECTION_ID,
        "provider": "google",
        "provider_identity_key": "google::synthetic-account",
        "current_version": 3,
        "status": MailDraftStatus.EDITING,
        "mode": MailMode.NEW,
        "source_thread_id": None,
        "source_message_id": None,
        "thread_headers": None,
        "to": ("recipient@example.test",),
        "cc": (),
        "bcc": (),
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
        "connection_status": "connected",
        "read_capability_status": CapabilityStatus.ENABLED,
        "write_capability_status": CapabilityStatus.ENABLED,
        "write_capability_error_code": None,
    }
    values.update(overrides)
    return ports.MailDraftSubmissionSnapshot(**values)


def _calendar_snapshot(ports: ModuleType, **overrides: object) -> object:
    """构造一个确认完整、无参会人且不发通知的创建提案。"""
    values: dict[str, object] = {
        "proposal_id": PROPOSAL_ID,
        "connection_id": GOOGLE_CONNECTION_ID,
        "provider": "google",
        "provider_identity_key": "google::synthetic-account",
        "calendar_id": "synthetic-calendar",
        "operation_kind": "create",
        "target_event_id": None,
        "base_etag": None,
        "before_snapshot_id": None,
        "current_provider_etag": None,
        "current_version": 4,
        "status": CalendarProposalStatus.EDITING,
        "title": "Synthetic meeting",
        "description": "Synthetic description",
        "location": "Synthetic room",
        "starts_at": "2030-01-02T09:00:00Z",
        "ends_at": "2030-01-02T10:00:00Z",
        "timezone": "UTC",
        "all_day": False,
        "attendees": (),
        "notification_policy": NotificationPolicy.NONE,
        "changed_fields": (),
        "submission_ready": True,
        "connection_status": "connected",
        "read_capability_status": CapabilityStatus.ENABLED,
        "write_capability_status": CapabilityStatus.ENABLED,
        "write_capability_error_code": None,
        "calendar_can_write": True,
        "target_event_can_edit": None,
        "target_event_recurring": None,
        "target_event_status": None,
    }
    values.update(overrides)
    return ports.CalendarProposalSubmissionSnapshot(**values)


def _ids() -> object:
    """按操作、任务、步骤、审批顺序提供固定 UUID。"""
    return iter((OPERATION_ID, TASK_ID, STEP_ID, APPROVAL_ID))


def _mail_use_case(
    ports: ModuleType,
    use_cases: ModuleType,
    transaction: _FakeTransaction,
    *,
    policy: object | None = None,
    preflights: _FakePreflights | None = None,
) -> tuple[object, _FakeCommandCipher, _FakePreflights]:
    """组装邮件提交用例及可观察 Fake 依赖。"""

    @asynccontextmanager
    async def transactions():
        yield transaction

    cipher = _FakeCommandCipher()
    selected_preflights = preflights or _FakePreflights(_FakePreflight(ports))
    ids = _ids()
    use_case = use_cases.SubmitMailDraftUseCase(
        transactions=transactions,
        preflights=selected_preflights,
        write_policy=policy or _FakeWritePolicy(),
        command_cipher=cipher,
        id_factory=lambda: next(ids),
    )
    return use_case, cipher, selected_preflights


def _calendar_use_case(
    ports: ModuleType,
    use_cases: ModuleType,
    transaction: _FakeTransaction,
    *,
    policy: object | None = None,
    preflights: _FakePreflights | None = None,
) -> tuple[object, _FakeCommandCipher, _FakePreflights]:
    """组装日程提交用例及可观察 Fake 依赖。"""

    @asynccontextmanager
    async def transactions():
        yield transaction

    cipher = _FakeCommandCipher()
    selected_preflights = preflights or _FakePreflights(_FakePreflight(ports))
    ids = _ids()
    use_case = use_cases.SubmitCalendarProposalUseCase(
        transactions=transactions,
        preflights=selected_preflights,
        write_policy=policy or _FakeWritePolicy(),
        command_cipher=cipher,
        id_factory=lambda: next(ids),
    )
    return use_case, cipher, selected_preflights


async def test_submit_mail_draft_freezes_one_encrypted_command() -> None:
    """提交必须产生单审批、十分钟窗口和不含正文的 JSONB marker。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(mail=_mail_snapshot(ports))
    submit_mail, cipher, preflights = _mail_use_case(ports, use_cases, transaction)

    result = await submit_mail.execute(
        user_id=USER_ID,
        draft_id=DRAFT_ID,
        expected_version=3,
        idempotency_key="synthetic-submit-1",
        now=NOW,
    )

    assert result.task_id == TASK_ID
    assert result.approval_id == APPROVAL_ID
    assert preflights.requested == [("google", "mail.send")]
    assert len(cipher.payloads) == 1
    assert len(transaction.created) == 1
    submission = transaction.created[0]
    assert submission.action == "mail.send"
    assert submission.expires_at == datetime(2030, 1, 1, 0, 10, tzinfo=UTC)
    assert submission.payload == {"storage": "encrypted", "schema_version": "mail_send.v1"}
    assert submission.encrypted_command.ciphertext == b"synthetic-ciphertext"


async def test_submit_mail_revalidates_recipient_limit_before_creating_facts() -> None:
    """即使持久快照被破坏为五十一人，严格命令边界也必须零事实拒绝。"""
    ports, use_cases = _trusted_action_modules()
    recipients = tuple(f"recipient-{index}@example.test" for index in range(51))
    transaction = _FakeTransaction(mail=_mail_snapshot(ports, to=recipients))
    submit_mail, cipher, _ = _mail_use_case(ports, use_cases, transaction)

    with pytest.raises(ValueError):
        await submit_mail.execute(
            user_id=USER_ID,
            draft_id=DRAFT_ID,
            expected_version=3,
            idempotency_key="synthetic-recipient-limit",
            now=NOW,
        )

    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_mail_binds_exact_connection_and_high_risk() -> None:
    """邮件审批必须绑定草稿连接且无条件标为 high。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(mail=_mail_snapshot(ports))
    submit_mail, cipher, preflights = _mail_use_case(ports, use_cases, transaction)

    await submit_mail.execute(
        user_id=USER_ID,
        draft_id=DRAFT_ID,
        expected_version=3,
        idempotency_key="synthetic-mail-risk",
        now=NOW,
    )

    submission = transaction.created[0]
    command = preflights.preflight.commands[0]  # type: ignore[union-attr]
    assert command.connection_id == GOOGLE_CONNECTION_ID
    assert cipher.payloads[0]["connection_id"] == str(GOOGLE_CONNECTION_ID)
    assert submission.risk_level.value == "high"


@pytest.mark.parametrize(
    ("policy", "expected_code"),
    (
        (
            type(
                "GlobalSwitchOff",
                (),
                {
                    "provider_writes_enabled": lambda self, provider: False,
                    "write_account_allowed": lambda self, provider_identity_key: True,
                },
            )(),
            "external_writes_disabled",
        ),
        (
            type(
                "AccountNotAllowed",
                (),
                {
                    "provider_writes_enabled": lambda self, provider: True,
                    "write_account_allowed": lambda self, provider_identity_key: False,
                },
            )(),
            "external_writes_disabled",
        ),
    ),
)
async def test_submit_mail_write_gate_failure_creates_zero_facts(
    policy: object,
    expected_code: str,
) -> None:
    """全局/供应商门禁或账户允许列表未通过时不得留下 task/approval。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(mail=_mail_snapshot(ports))
    submit_mail, cipher, _ = _mail_use_case(
        ports,
        use_cases,
        transaction,
        policy=policy,
    )

    with pytest.raises(StateConflictError) as raised:
        await submit_mail.execute(
            user_id=USER_ID,
            draft_id=DRAFT_ID,
            expected_version=3,
            idempotency_key="synthetic-disabled-write",
            now=NOW,
        )

    assert raised.value.error_code == expected_code
    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_mail_rechecks_capability_after_lock() -> None:
    """冻结前丢失 mail.send 时必须使用稳定能力冲突且零审批。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(
        mail=_mail_snapshot(
            ports,
            write_capability_status=CapabilityStatus.DISABLED,
        )
    )
    submit_mail, cipher, _ = _mail_use_case(ports, use_cases, transaction)

    with pytest.raises(StateConflictError) as raised:
        await submit_mail.execute(
            user_id=USER_ID,
            draft_id=DRAFT_ID,
            expected_version=3,
            idempotency_key="synthetic-capability-loss",
            now=NOW,
        )

    assert raised.value.error_code == "connection_capability_disabled"
    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_mail_rejects_consumed_version_for_a_new_key() -> None:
    """pending、拒绝、过期或失效审批引用过的草稿版本都永久不可再次提交。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(mail=_mail_snapshot(ports), consumed=True)
    submit_mail, _, _ = _mail_use_case(ports, use_cases, transaction)

    with pytest.raises(StateConflictError) as raised:
        await submit_mail.execute(
            user_id=USER_ID,
            draft_id=DRAFT_ID,
            expected_version=3,
            idempotency_key="synthetic-new-key-after-terminal",
            now=NOW,
        )

    assert raised.value.error_code == "draft_version_conflict"
    assert transaction.created == []


async def test_submit_reuses_same_idempotency_task_before_locking_version() -> None:
    """同用户同提交键必须先返回既有 task，不能因当前版本后来变化创建第二审批。"""
    ports, use_cases = _trusted_action_modules()
    existing = ports.ExistingTrustedActionSubmission(
        task_id=TASK_ID,
        approval_id=APPROVAL_ID,
        operation_id=OPERATION_ID,
        proposal_kind="mail_draft",
        proposal_id=DRAFT_ID,
        proposal_version=3,
        status=TaskStatus.WAITING_APPROVAL,
    )
    transaction = _FakeTransaction(existing=existing)
    submit_mail, cipher, _ = _mail_use_case(ports, use_cases, transaction)

    result = await submit_mail.execute(
        user_id=USER_ID,
        draft_id=DRAFT_ID,
        expected_version=3,
        idempotency_key="synthetic-replay",
        now=NOW,
    )

    assert result.task_id == TASK_ID
    assert result.approval_id == APPROVAL_ID
    assert result.status is TaskStatus.WAITING_APPROVAL
    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_fails_closed_when_provider_action_is_unavailable() -> None:
    """未实现的精确 provider/action 不能静默生成可批准命令。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(mail=_mail_snapshot(ports))
    preflights = _FakePreflights(None)
    submit_mail, cipher, _ = _mail_use_case(
        ports,
        use_cases,
        transaction,
        preflights=preflights,
    )

    with pytest.raises(StateConflictError) as raised:
        await submit_mail.execute(
            user_id=USER_ID,
            draft_id=DRAFT_ID,
            expected_version=3,
            idempotency_key="synthetic-provider-unavailable",
            now=NOW,
        )

    assert raised.value.error_code == "provider_action_unavailable"
    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_calendar_binds_exact_target_and_medium_risk() -> None:
    """个人日程创建冻结精确连接/日历、派生 client ID，并标为 medium。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(calendar=_calendar_snapshot(ports))
    submit_calendar, cipher, preflights = _calendar_use_case(
        ports,
        use_cases,
        transaction,
    )

    result = await submit_calendar.execute(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        expected_version=4,
        idempotency_key="synthetic-calendar-submit",
        now=NOW,
    )

    assert result.task_id == TASK_ID
    submission = transaction.created[0]
    command = preflights.preflight.commands[0]  # type: ignore[union-attr]
    assert command.connection_id == GOOGLE_CONNECTION_ID
    assert command.calendar_id == "synthetic-calendar"
    assert command.client_event_id.startswith("a")
    assert cipher.payloads[0]["connection_id"] == str(GOOGLE_CONNECTION_ID)
    assert cipher.payloads[0]["calendar_id"] == "synthetic-calendar"
    assert submission.action == "calendar.create"
    assert submission.risk_level.value == "medium"
    assert submission.expires_at == NOW + timedelta(minutes=10)


async def test_submit_calendar_revalidates_attendee_limit() -> None:
    """被破坏为五十一参会人的 snapshot 必须在预检和加密前失败。"""
    ports, use_cases = _trusted_action_modules()
    attendees = tuple(f"attendee-{index}@example.test" for index in range(51))
    transaction = _FakeTransaction(calendar=_calendar_snapshot(ports, attendees=attendees))
    submit_calendar, cipher, preflights = _calendar_use_case(
        ports,
        use_cases,
        transaction,
    )

    with pytest.raises(ValueError):
        await submit_calendar.execute(
            user_id=USER_ID,
            proposal_id=PROPOSAL_ID,
            expected_version=4,
            idempotency_key="synthetic-attendee-limit",
            now=NOW,
        )

    assert transaction.created == []
    assert cipher.payloads == []
    assert preflights.preflight.commands == []  # type: ignore[union-attr]


async def test_submit_calendar_rejects_stale_etag() -> None:
    """修改提案的冻结 ETag 与本地最新事件不同则不得创建审批。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(
        calendar=_calendar_snapshot(
            ports,
            operation_kind="update",
            target_event_id="synthetic-event",
            base_etag='"etag-old"',
            before_snapshot_id=BEFORE_SNAPSHOT_ID,
            current_provider_etag='"etag-new"',
            changed_fields=("title",),
            target_event_can_edit=True,
            target_event_recurring=False,
            target_event_status="confirmed",
        )
    )
    submit_calendar, cipher, _ = _calendar_use_case(ports, use_cases, transaction)

    with pytest.raises(StateConflictError) as raised:
        await submit_calendar.execute(
            user_id=USER_ID,
            proposal_id=PROPOSAL_ID,
            expected_version=4,
            idempotency_key="synthetic-stale-etag",
            now=NOW,
        )

    assert raised.value.error_code == "calendar_event_version_conflict"
    assert transaction.created == []
    assert cipher.payloads == []


async def test_submit_calendar_rechecks_directory_and_capability() -> None:
    """冻结前 calendar.write 或精确目录写权限丢失都必须 fail closed。"""
    ports, use_cases = _trusted_action_modules()
    for overrides in (
        {"write_capability_status": CapabilityStatus.REVOKED},
        {"calendar_can_write": False},
    ):
        transaction = _FakeTransaction(calendar=_calendar_snapshot(ports, **overrides))
        submit_calendar, cipher, _ = _calendar_use_case(
            ports,
            use_cases,
            transaction,
        )

        with pytest.raises(StateConflictError) as raised:
            await submit_calendar.execute(
                user_id=USER_ID,
                proposal_id=PROPOSAL_ID,
                expected_version=4,
                idempotency_key=f"synthetic-calendar-capability-{len(overrides)}",
                now=NOW,
            )

        assert raised.value.error_code in {
            "connection_scope_missing",
            "connection_capability_disabled",
        }
        assert transaction.created == []
        assert cipher.payloads == []


async def test_submit_calendar_rejects_consumed_version_for_a_new_key() -> None:
    """拒绝、过期或撤回后必须先保存新版本，原提案版本永久 consumed。"""
    ports, use_cases = _trusted_action_modules()
    transaction = _FakeTransaction(calendar=_calendar_snapshot(ports), consumed=True)
    submit_calendar, _, _ = _calendar_use_case(ports, use_cases, transaction)

    with pytest.raises(StateConflictError) as raised:
        await submit_calendar.execute(
            user_id=USER_ID,
            proposal_id=PROPOSAL_ID,
            expected_version=4,
            idempotency_key="synthetic-calendar-new-key",
            now=NOW,
        )

    assert raised.value.error_code == "proposal_version_conflict"
    assert transaction.created == []
