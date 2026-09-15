"""为真实浏览器编辑器建立每例独立的加密合成来源，不创建审批或执行结果。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from ai_employee.application.calendar_event_aad import calendar_event_field_aad_v2
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.trusted_actions import synthetic_account_id


@dataclass(frozen=True, slots=True)
class M2Source:
    """只返回来源标识和未来合成时间，正文、凭据及 provider 响应不离开服务。"""

    connection_id: UUID
    calendar_id: str
    thread_id: UUID
    message_id: UUID
    event_id: UUID
    starts_at: datetime
    ends_at: datetime


async def seed_m2_source(
    *, sessions: ManagedAsyncSessionMaker, master_key_file: Path, user_id: UUID,
    provider: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> M2Source:
    """创建带四项能力、正确 AEAD 和回复来源头的全新合成账户。

    Args:
        sessions: 双开关组合根拥有的数据库工厂，事务失败全部回滚。
        master_key_file: 本地测试密钥；仅用于正式 AEAD，不进入输出。
        user_id: 当前认证用户；所有直接数据和父实体都显式归属该用户。
        provider: HTTP 封闭枚举，只允许 Google/Microsoft。
        clock: 可替换 UTC 时钟；事件放在未来工作日，测试不依赖宿主机时区。

    Returns:
        精确本例标识；浏览器须另经生产 API 创建/编辑/提交/批准动作。
    """
    connection_id, thread_id, message_id, event_id = uuid4(), uuid4(), uuid4(), uuid4()
    account_id = synthetic_account_id(provider, connection_id)
    current = clock().astimezone(UTC)
    starts = (current + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    while starts.weekday() >= 5:
        starts += timedelta(days=1)
    ends = starts + timedelta(minutes=30)
    calendar_id, provider_event_id = f"synthetic-calendar-{connection_id}", f"synthetic-event-{event_id}"
    provider_message_id = f"synthetic-message-{message_id}"
    account_email = f"synthetic-{connection_id}@example.test"
    scopes = (
        {"mail.read": "https://www.googleapis.com/auth/gmail.readonly",
         "mail.send": "https://www.googleapis.com/auth/gmail.send",
         "calendar.read": "https://www.googleapis.com/auth/calendar.readonly",
         "calendar.write": "https://www.googleapis.com/auth/calendar.events"}
        if provider == "google" else
        {"mail.read": "Mail.Read", "mail.send": "Mail.Send",
         "calendar.read": "Calendars.Read", "calendar.write": "Calendars.ReadWrite"}
    )
    cipher = AeadCipher.from_file(master_key_file)
    description = cipher.encrypt(b"Synthetic source description", calendar_event_field_aad_v2(
        user_id=str(user_id), connection_id=str(connection_id), calendar_id=calendar_id,
        provider_event_id=provider_event_id, field="description",
    ))
    location = cipher.encrypt(b"Synthetic source room", calendar_event_field_aad_v2(
        user_id=str(user_id), connection_id=str(connection_id), calendar_id=calendar_id,
        provider_event_id=provider_event_id, field="location",
    ))
    body = cipher.encrypt(b"Synthetic source body", f"{user_id}:{connection_id}:{provider_message_id}:body".encode("ascii"))
    async with sessions.begin() as session:
        session.add(OAuthConnectionModel(
            id=connection_id, user_id=user_id, provider=provider, provider_account_id=account_id,
            provider_tenant_id="synthetic-tenant" if provider == "microsoft" else "",
            account_type="personal" if provider == "microsoft" else "google", account_email=account_email,
            scopes=list(scopes.values()), status="connected",
        ))
        await session.flush()
        for kind in ("access_token", "refresh_token"):
            encrypted = cipher.encrypt(f"synthetic-m2-{kind}".encode("ascii"), f"{user_id}:{connection_id}:{kind}".encode("ascii"))
            session.add(EncryptedCredentialModel(
                user_id=user_id, connection_id=connection_id, credential_kind=kind,
                ciphertext=encrypted.ciphertext, nonce=encrypted.nonce, key_version=encrypted.key_version,
                token_expires_at=current + timedelta(hours=1) if kind == "access_token" else None,
            ))
        for capability, scope in scopes.items():
            session.add(ConnectionCapabilityModel(
                user_id=user_id, connection_id=connection_id, capability=capability, status="enabled",
                actual_scopes=[scope], last_verified_at=current,
            ))
        session.add(ProviderCalendarModel(
            user_id=user_id, connection_id=connection_id, provider_calendar_id=calendar_id,
            name="Synthetic M2 calendar", timezone="UTC", is_primary=True, access_role="owner",
            can_write=True, provider_url="https://example.test/calendar",
        ))
        session.add(SyncCursorModel(
            connection_id=connection_id, resource_kind="calendar", scope_key=calendar_id,
            cursor="synthetic-cursor", last_success_at=current,
        ))
        session.add(SyncCursorModel(
            connection_id=connection_id, resource_kind="calendar", scope_key="directory",
            cursor="synthetic-directory-cursor" if provider == "google" else None,
            last_success_at=current,
        ))
        session.add(EmailThreadModel(
            id=thread_id, user_id=user_id, connection_id=connection_id,
            provider_thread_id=f"synthetic-thread-{thread_id}", subject="Synthetic source subject",
            participants=[{"email": "sender@example.test"}], latest_message_at=current,
            provider_url="https://example.test/thread",
        ))
        await session.flush()
        session.add(EmailMessageModel(
            id=message_id, user_id=user_id, connection_id=connection_id, thread_id=thread_id,
            provider_message_id=provider_message_id, internet_message_id=f"<{message_id}@example.test>",
            received_at=current, sender={"email": "sender@example.test"},
            recipients=[{"email": account_email, "kind": "to"}, {"email": "copy@example.test", "kind": "cc"}],
            subject="Synthetic source subject", snippet="Synthetic source snippet",
            body_ciphertext=body.ciphertext, body_nonce=body.nonce, body_key_version=body.key_version,
            labels=[], headers={"message-id": f"<{message_id}@example.test>", "references": "",
                "in-reply-to": "", "from": "sender@example.test", "reply-to": "",
                "to": account_email, "cc": "copy@example.test", "bcc": ""},
            provider_url="https://example.test/message",
        ))
        session.add(CalendarEventModel(
            id=event_id, user_id=user_id, connection_id=connection_id, provider_event_id=provider_event_id,
            calendar_id=calendar_id, title="Synthetic source meeting",
            description_ciphertext=description.ciphertext, description_nonce=description.nonce,
            description_key_version=description.key_version, description_aad_version=2,
            location_ciphertext=location.ciphertext, location_nonce=location.nonce,
            location_key_version=location.key_version, location_aad_version=2,
            starts_at=starts, ends_at=ends, all_day=False, transparency="opaque", status="confirmed",
            timezone="UTC", etag='"synthetic-v1"', organizer={"email": account_email}, attendees=[],
            access_role="owner", can_edit=True, provider_url="https://example.test/event",
        ))
    return M2Source(connection_id, calendar_id, thread_id, message_id, event_id, starts, ends)
