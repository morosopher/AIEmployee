"""验证邮件真实写命令的规范化、回复绑定与不可变领域契约。"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_employee.domain.mail_actions import (
    MailMode,
    MailSendCommand,
    ReplyThreadHeaders,
    normalize_mail_recipients,
    normalize_mailbox_address,
)

OPERATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")
DRAFT_ID = UUID("00000000-0000-0000-0000-000000000003")
MESSAGE_DATE = datetime(2026, 8, 6, 9, 30, tzinfo=UTC)
THREAD_HEADERS = ReplyThreadHeaders(
    in_reply_to="<source-message@example.test>",
    references=("<root-message@example.test>", "<source-message@example.test>"),
)


def _mail_command(**overrides: object) -> MailSendCommand:
    """创建一个只使用合成数据的有效领域命令，并允许测试覆盖单个字段。"""
    values: dict[str, object] = {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": OPERATION_ID,
        "connection_id": CONNECTION_ID,
        "draft_id": DRAFT_ID,
        "draft_version": 1,
        "message_date": MESSAGE_DATE,
        "mode": MailMode.NEW,
        "source_thread_id": None,
        "source_message_id": None,
        "to": ("owner@example.test",),
        "cc": (),
        "bcc": (),
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
        "thread_headers": None,
    }
    values.update(overrides)
    return MailSendCommand(**values)  # type: ignore[arg-type]


def test_mail_mode_exposes_only_supported_m2_modes() -> None:
    """邮件命令只能表达新邮件、回复和全部回复。"""
    assert tuple(MailMode) == (MailMode.NEW, MailMode.REPLY, MailMode.REPLY_ALL)


def test_mailbox_normalization_casefolds_only_domain() -> None:
    """域名大小写应归一，而本地部分的大小写、点号与加号语义必须保留。"""
    assert normalize_mailbox_address("Owner.Name+tag@Example.TEST") == (
        "Owner.Name+tag@example.test"
    )
    assert normalize_mailbox_address("ownername+tag@example.test") != (
        normalize_mailbox_address("owner.name+tag@example.test")
    )


@pytest.mark.parametrize(
    "address",
    (
        "owner@example.test",
        "owner(comment)@EXAMPLE.TEST",
        '"owner"@example.test',
    ),
)
def test_mailbox_normalization_collapses_equivalent_addr_spec_representations(
    address: str,
) -> None:
    """CFWS、注释与不必要引号不得制造语义相同但可绕过去重的地址。"""
    assert normalize_mailbox_address(address) == "owner@example.test"


def test_mailbox_normalization_preserves_required_quoted_local_part_semantics() -> None:
    """真正需要引号和转义的 local part 应重建为合法且再次规范化稳定的 addr-spec。"""
    normalized_space = normalize_mailbox_address('"owner name"@Example.TEST')
    normalized_quote = normalize_mailbox_address('"quoted\\"name"@Example.TEST')

    assert normalized_space == '"owner name"@example.test'
    assert normalized_quote == '"quoted\\"name"@example.test'
    assert normalize_mailbox_address(normalized_space) == normalized_space
    assert normalize_mailbox_address(normalized_quote) == normalized_quote


def test_recipients_are_deduplicated_deterministically_across_fields() -> None:
    """To、CC、BCC 应按首次出现位置去重，不能让同一地址收到多份邮件。"""
    to, cc, bcc = normalize_mail_recipients(
        ("owner@Example.test", "Owner@example.test"),
        ("owner(comment)@example.TEST", "second@example.test"),
        ('"owner"@example.test', "second@EXAMPLE.TEST", "third@example.test"),
    )

    assert to == ("owner@example.test", "Owner@example.test")
    assert cc == ("second@example.test",)
    assert bcc == ("third@example.test",)


@pytest.mark.parametrize(
    "address",
    (
        "missing-at.example.test",
        "@example.test",
        "owner@",
        "Owner <owner@example.test>",
        "owner@example.test\r\nBcc: injected@example.test",
    ),
)
def test_mailbox_normalization_rejects_invalid_or_injectable_addresses(address: str) -> None:
    """地址必须是单个 addr-spec，且任何 CR/LF 注入都应在领域边界失败。"""
    with pytest.raises(ValueError):
        normalize_mailbox_address(address)


def test_mailbox_normalization_sanitizes_stdlib_parser_failures() -> None:
    """stdlib 对残缺 domain literal 的内部异常必须收敛为固定脱敏领域错误。"""
    malformed = "a@["

    with pytest.raises(ValueError) as captured:
        normalize_mailbox_address(malformed)

    assert type(captured.value) is ValueError
    assert str(captured.value) == "mailbox address must be a valid addr-spec"
    assert malformed not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_reply_thread_headers_are_strict_and_frozen() -> None:
    """回复头只携带规范 Message-ID 值，构造后不可被审批后修改。"""
    headers = ReplyThreadHeaders(
        in_reply_to="<source@example.test>",
        references=("<root@example.test>", "<source@example.test>"),
    )

    assert headers.references[-1] == headers.in_reply_to
    with pytest.raises(FrozenInstanceError):
        headers.in_reply_to = "<changed@example.test>"  # type: ignore[misc]


def test_reply_thread_headers_require_direct_parent_as_last_reference() -> None:
    """引用链末项必须是直接回复对象，防止审批命令脱离同步到的父邮件。"""
    with pytest.raises(ValueError, match="last reference"):
        ReplyThreadHeaders(
            in_reply_to="<source@example.test>",
            references=("<source@example.test>", "<different@example.test>"),
        )


@pytest.mark.parametrize(
    "message_id",
    (
        "<source.message+tag@example.test>",
        "<left!#$%&'*+-/=?^_`{|}~@example.test>",
        "<source@[127.0.0.1]>",
    ),
)
def test_reply_thread_headers_accept_canonical_message_ids(message_id: str) -> None:
    """规范 dot-atom 标点与合法 domain literal 应可作为应用生成的 Message-ID。"""
    headers = ReplyThreadHeaders(in_reply_to=message_id, references=(message_id,))

    assert headers.in_reply_to == message_id


@pytest.mark.parametrize(
    "message_id",
    (
        "<one,other@example.test>",
        "<one(comment)@example.test>",
        "<one\\other@example.test>",
        "<one\x00@example.test>",
        "<用户@example.test>",
        "<one@example.test>,<two@example.test>",
        "<one@example.test> <two@example.test>",
        " <one@example.test>",
        "<one@example.test> ",
        "<one @example.test>",
        '<"one"@example.test>',
    ),
)
def test_reply_thread_headers_reject_noncanonical_message_ids(message_id: str) -> None:
    """Message-ID 必须是单个、无 CFWS/defect、可打印 ASCII 的规范表示。"""
    with pytest.raises(ValueError):
        ReplyThreadHeaders(in_reply_to=message_id, references=(message_id,))


def test_reply_thread_headers_sanitize_stdlib_parser_failures() -> None:
    """残缺 Message-ID 触发的 stdlib 内部异常不能越过领域边界。"""
    malformed = "<J@[>"

    with pytest.raises(ValueError) as captured:
        ReplyThreadHeaders(in_reply_to=malformed, references=(malformed,))

    assert type(captured.value) is ValueError
    assert str(captured.value) == "reply thread header must be a canonical Message-ID"
    assert malformed not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("in_reply_to", "references"),
    (
        ("source@example.test", ("<source@example.test>",)),
        ("<source@example.test>\r\nBcc: injected@example.test", ("<source@example.test>",)),
        ("<source@example.test>", ()),
        ("<source@example.test>", ("not-a-message-id",)),
        (
            "<source@example.test>",
            ("<source@example.test>", "<source@example.test>"),
        ),
    ),
)
def test_reply_thread_headers_reject_untrusted_values(
    in_reply_to: str,
    references: tuple[str, ...],
) -> None:
    """任意 Header 文本、空引用链和重复引用不能进入冻结回复命令。"""
    with pytest.raises(ValueError):
        ReplyThreadHeaders(in_reply_to=in_reply_to, references=references)


def test_mail_send_command_has_exact_fields_and_no_from_header() -> None:
    """发件身份只能来自连接事实，领域命令不得暴露用户可伪造的 From 字段。"""
    assert tuple(field.name for field in fields(MailSendCommand)) == (
        "schema_version",
        "action",
        "operation_id",
        "connection_id",
        "draft_id",
        "draft_version",
        "message_date",
        "mode",
        "source_thread_id",
        "source_message_id",
        "to",
        "cc",
        "bcc",
        "subject",
        "body_text",
        "thread_headers",
    )


def test_mail_send_command_is_frozen_and_normalizes_recipients() -> None:
    """冻结命令应持有规范化 tuple，避免审批后地址顺序或域名大小写漂移。"""
    command = _mail_command(
        to=("owner@Example.test",),
        cc=("owner@example.test", "second@Example.test"),
    )

    assert command.to == ("owner@example.test",)
    assert command.cc == ("second@example.test",)
    with pytest.raises(FrozenInstanceError):
        command.subject = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize("mode", (MailMode.REPLY, MailMode.REPLY_ALL))
def test_reply_modes_require_complete_source_binding(mode: MailMode) -> None:
    """回复与全部回复必须同时冻结线程、源邮件和严格引用头。"""
    command = _mail_command(
        mode=mode,
        source_thread_id="thread-synthetic",
        source_message_id="message-synthetic",
        thread_headers=THREAD_HEADERS,
    )

    assert command.mode is mode


@pytest.mark.parametrize("missing_field", ("source_thread_id", "source_message_id", "thread_headers"))
def test_reply_modes_reject_missing_source_binding(missing_field: str) -> None:
    """回复绑定任一组成部分缺失时都不能形成可审批命令。"""
    overrides: dict[str, object] = {
        "mode": MailMode.REPLY,
        "source_thread_id": "thread-synthetic",
        "source_message_id": "message-synthetic",
        "thread_headers": THREAD_HEADERS,
        missing_field: None,
    }

    with pytest.raises(ValueError):
        _mail_command(**overrides)


@pytest.mark.parametrize("bound_field", ("source_thread_id", "source_message_id", "thread_headers"))
def test_new_mode_rejects_reply_source_binding(bound_field: str) -> None:
    """新邮件不能暗带回复线程绑定或引用头。"""
    value: object = THREAD_HEADERS if bound_field == "thread_headers" else "synthetic-source"

    with pytest.raises(ValueError):
        _mail_command(**{bound_field: value})


def test_mail_command_requires_one_to_fifty_unique_recipients() -> None:
    """去重后的跨字段收件人总数必须位于安全边界 1..50。"""
    with pytest.raises(ValueError):
        _mail_command(to=(), cc=(), bcc=())

    exactly_fifty = tuple(f"recipient-{index}@example.test" for index in range(50))
    assert len(_mail_command(to=exactly_fifty).to) == 50

    too_many = exactly_fifty + ("recipient-50@example.test",)
    with pytest.raises(ValueError):
        _mail_command(to=too_many)


def test_mail_command_enforces_unicode_text_limits() -> None:
    """主题和正文按 Unicode 字符计数，分别限制为 255 与 100000。"""
    assert len(_mail_command(subject="会" * 255).subject) == 255
    assert len(_mail_command(body_text="文" * 100_000).body_text) == 100_000

    with pytest.raises(ValueError):
        _mail_command(subject="会" * 256)
    with pytest.raises(ValueError):
        _mail_command(body_text="文" * 100_001)


def test_mail_command_requires_aware_message_date_and_safe_subject() -> None:
    """邮件日期必须可确定绝对时刻，主题不得携带可注入其他 Header 的换行。"""
    with pytest.raises(ValueError):
        _mail_command(message_date=MESSAGE_DATE.replace(tzinfo=None))
    with pytest.raises(ValueError):
        _mail_command(subject="Synthetic\r\nBcc: injected@example.test")


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("schema_version", "mail_send.v2"),
        ("action", "mail.forward"),
        ("draft_version", 0),
    ),
)
def test_mail_command_rejects_untrusted_protocol_values(
    field_name: str,
    invalid_value: object,
) -> None:
    """固定协议值或草稿版本非法时不得构造领域命令。"""
    with pytest.raises(ValueError):
        _mail_command(**{field_name: invalid_value})
