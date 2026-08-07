"""验证 M2 敏感操作内容的规范 JSON 与记录绑定 AAD。"""

from collections.abc import ItemsView, Iterator, Mapping
from typing import cast
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.infrastructure.security.action_payloads import (
    ActionPayloadCipher,
    ActionPayloadFormatError,
    action_payload_aad,
)
from ai_employee.infrastructure.security.encryption import AeadCipher

USER_ID = UUID("00000000-0000-0000-0000-000000000601")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000602")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000603")
OTHER_APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000604")
SENSITIVE_FORMAT_MARKER = "synthetic-sensitive-action-body-marker"


class _ExplodingMapping(Mapping[str, object]):
    """在 ``items`` 被调用时抛出携带合成敏感标记的自定义异常。"""

    def __init__(self, marker: str) -> None:
        self._marker = marker
        self.items_calls = 0

    def __getitem__(self, key: str) -> object:
        if key != "body_text":
            raise KeyError(key)
        return self._marker

    def __iter__(self) -> Iterator[str]:
        return iter(("body_text",))

    def __len__(self) -> int:
        return 1

    def items(self) -> ItemsView[str, object]:
        self.items_calls += 1
        raise RuntimeError(f"custom mapping exposed {self._marker}")


def _assert_format_error_is_content_free(error: BaseException) -> None:
    """验证固定格式错误不保留正文、解析器对象或隐式异常链。"""
    assert type(error) is ActionPayloadFormatError
    assert str(error) == "action payload format is invalid"
    assert SENSITIVE_FORMAT_MARKER not in str(error)
    assert SENSITIVE_FORMAT_MARKER not in repr(error)
    assert not hasattr(error, "object")
    assert not hasattr(error, "doc")
    assert error.__cause__ is None
    assert error.__context__ is None


def _cyclic_payload() -> dict[str, object]:
    """构造含合成标记的直接循环 JSON object 候选。"""
    payload: dict[str, object] = {"marker": SENSITIVE_FORMAT_MARKER}
    payload["body_text"] = [payload]
    return payload


def test_encrypted_command_cannot_move_between_approval_records() -> None:
    """同一用户下替换 ApprovalRequest ID 也必须破坏认证标签。"""
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


@pytest.mark.parametrize(
    ("user_id", "record_id", "content_kind", "action", "schema_version"),
    (
        (
            OTHER_USER_ID,
            APPROVAL_ID,
            "approval_command",
            "mail.send",
            "mail_send.v1",
        ),
        (
            USER_ID,
            OTHER_APPROVAL_ID,
            "approval_command",
            "mail.send",
            "mail_send.v1",
        ),
        (
            USER_ID,
            APPROVAL_ID,
            "mail_draft_body",
            "mail.send",
            "mail_send.v1",
        ),
        (
            USER_ID,
            APPROVAL_ID,
            "approval_command",
            "calendar.create",
            "mail_send.v1",
        ),
        (
            USER_ID,
            APPROVAL_ID,
            "approval_command",
            "mail.send",
            "calendar_create.v1",
        ),
    ),
)
def test_every_aad_dimension_is_authenticated(
    user_id: UUID,
    record_id: UUID,
    content_kind: str,
    action: str,
    schema_version: str,
) -> None:
    """用户、记录、内容类别、动作或 Schema 任一变化都不得解密。"""
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
            user_id=user_id,
            record_id=record_id,
            content_kind=content_kind,
            action=action,
            schema_version=schema_version,
        )


def test_encrypt_json_uses_exact_canonical_utf8_object_bytes() -> None:
    """键排序、紧凑分隔符和未转义 Unicode 必须形成唯一明文字节。"""
    key = b"k" * 32
    cipher = ActionPayloadCipher.from_key(key, key_version=7)
    value = cipher.encrypt_json(
        {"z": "合成正文", "a": {"second": 2, "first": 1}},
        user_id=USER_ID,
        record_id=APPROVAL_ID,
        content_kind="mail_draft_body",
        action="mail.draft",
        schema_version="mail_draft_body.v1",
    )
    aad = action_payload_aad(
        user_id=USER_ID,
        record_id=APPROVAL_ID,
        content_kind="mail_draft_body",
        action="mail.draft",
        schema_version="mail_draft_body.v1",
    )

    assert value.key_version == 7
    assert AeadCipher(key, key_version=7).decrypt(value, aad) == (
        b'{"a":{"first":1,"second":2},"z":"\xe5\x90\x88\xe6\x88\x90\xe6\xad\xa3\xe6\x96\x87"}'
    )
    assert cipher.decrypt_json(
        value,
        user_id=USER_ID,
        record_id=APPROVAL_ID,
        content_kind="mail_draft_body",
        action="mail.draft",
        schema_version="mail_draft_body.v1",
    ) == {"a": {"first": 1, "second": 2}, "z": "合成正文"}


@pytest.mark.parametrize(
    "payload",
    (
        {"body_text": f"{SENSITIVE_FORMAT_MARKER}\ud800"},
        {"body_text": [object()], "marker": SENSITIVE_FORMAT_MARKER},
        {"body_text": float("nan"), "marker": SENSITIVE_FORMAT_MARKER},
        {"body_text": float("inf"), "marker": SENSITIVE_FORMAT_MARKER},
        {"body_text": (SENSITIVE_FORMAT_MARKER,)},
        cast(dict[str, object], {1: SENSITIVE_FORMAT_MARKER}),
        {f"{SENSITIVE_FORMAT_MARKER}\ud800": "value"},
        _cyclic_payload(),
        _ExplodingMapping(SENSITIVE_FORMAT_MARKER),
    ),
)
def test_encrypt_json_replaces_format_errors_with_content_free_exception(
    payload: Mapping[str, object],
) -> None:
    """不支持值、非有限数与代理码位都只能产生固定无内容错误。"""
    cipher = ActionPayloadCipher.from_key(b"k" * 32)

    with pytest.raises((RuntimeError, TypeError, ValueError)) as raised:
        cipher.encrypt_json(
            payload,
            user_id=USER_ID,
            record_id=APPROVAL_ID,
            content_kind="mail_draft_body",
            action="mail.draft",
            schema_version="mail_draft_body.v1",
        )

    _assert_format_error_is_content_free(raised.value)
    if type(payload) is _ExplodingMapping:
        assert payload.items_calls == 0


@pytest.mark.parametrize(
    "plaintext",
    (
        b"\xff" + SENSITIVE_FORMAT_MARKER.encode("utf-8"),
        b'{"body_text":"' + SENSITIVE_FORMAT_MARKER.encode("utf-8"),
        b'["' + SENSITIVE_FORMAT_MARKER.encode("utf-8") + b'"]',
        b'{"description":NaN}',
        b'{"description":Infinity}',
        b'{"description":-Infinity}',
        b'{"description":"' + SENSITIVE_FORMAT_MARKER.encode("utf-8") + b'\\ud800"}',
        b'{"description":' + (b"1" * 4301) + b"}",
    ),
)
def test_decrypt_json_replaces_authenticated_format_errors_with_content_free_exception(
    plaintext: bytes,
) -> None:
    """认证后的非法或非标准 JSON object 不得携带解密内容越界。"""
    key = b"k" * 32
    aad = action_payload_aad(
        user_id=USER_ID,
        record_id=APPROVAL_ID,
        content_kind="calendar_snapshot",
        action="calendar.snapshot",
        schema_version="calendar_snapshot.v1",
    )
    encrypted = AeadCipher(key).encrypt(plaintext, aad)
    cipher = ActionPayloadCipher.from_key(key)

    with pytest.raises((TypeError, ValueError)) as raised:
        cipher.decrypt_json(
            EncryptedValue(encrypted.ciphertext, encrypted.nonce, encrypted.key_version),
            user_id=USER_ID,
            record_id=APPROVAL_ID,
            content_kind="calendar_snapshot",
            action="calendar.snapshot",
            schema_version="calendar_snapshot.v1",
        )

    _assert_format_error_is_content_free(raised.value)
