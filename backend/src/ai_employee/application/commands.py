"""定义四种 M2 可信命令的严格 JSON 边界、领域映射与规范哈希。"""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from ai_employee.domain.calendar_actions import (
    CalendarCommand,
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
)
from ai_employee.domain.mail_actions import (
    MailMode,
    MailSendCommand,
    ReplyThreadHeaders,
)

_RFC3339_DATETIME_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?"
    r"(?:Z|(?P<offset_sign>[+-])(?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))$"
)
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_strict_rfc3339_datetime(value: object) -> datetime:
    """把 M2 允许的严格 RFC3339 文本解析为 aware ``datetime``。

    M2 的冻结命令精度明确限制为微秒：小数秒只能包含一至六位，超过六位必须
    拒绝，不能依赖 ``datetime.fromisoformat`` 静默截断后让不同输入得到同一哈希。
    数字 offset 的小时范围为 00..23、分钟为 00..59；``-00:00`` 在 RFC3339
    表示未知本地 offset，并非可绑定的明确瞬间，因此与其他非法 offset 一并拒绝。

    Args:
        value: 从标准 JSON 解码得到的候选 datetime 值。

    Returns:
        通过严格词法、offset 范围和标准库实际日期时间范围校验的 aware datetime。

    Raises:
        ValueError: 值不是 M2 可无损表示的严格 RFC3339 datetime。
    """
    if not isinstance(value, str):
        # Pydantic v2 不会把 validator 的 TypeError 包装为 ValidationError；这里必须保留
        # ValueError，才能由应用边界统一替换为不含原始输入的脱敏稳定错误。
        raise ValueError("datetime must use strict RFC 3339 syntax")  # noqa: TRY004
    matched = _RFC3339_DATETIME_PATTERN.fullmatch(value)
    if matched is None:
        raise ValueError("datetime must use strict RFC 3339 syntax")

    offset_sign = matched.group("offset_sign")
    if offset_sign is not None:
        offset_hour = int(matched.group("offset_hour"))
        offset_minute = int(matched.group("offset_minute"))
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError("datetime offset must be within RFC 3339 range")
        if offset_sign == "-" and offset_hour == 0 and offset_minute == 0:
            raise ValueError("datetime offset must identify a known instant")

    # Python 使用 ``+00:00`` 表示 UTC；词法和 offset 范围验证完成后才转换标准 Z。
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
        offset = parsed.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError("datetime must be a valid RFC 3339 value") from None
    if parsed.tzinfo is None or offset is None:
        raise ValueError("datetime must include an RFC 3339 timezone")
    return parsed


class TrustedCommandValidationError(ValueError):
    """表示可信命令未通过应用边界验证，且不携带任何原始输入细节。"""

    def __init__(self) -> None:
        """使用稳定、可公开且不含正文、描述或地址的固定错误消息。"""
        super().__init__("trusted command validation failed")


class _StrictCommandSchema(BaseModel):
    """为所有可信命令提供禁止额外字段且禁止 Python 宽松转换的共同配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ReplyThreadHeadersSchema(_StrictCommandSchema):
    """回复所需的严格内部引用头 Schema，只允许两个明确字段。"""

    in_reply_to: str
    references: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_domain_contract(self) -> Self:
        """复用纯领域约束验证 Message-ID 结构、引用非空与去重。"""
        ReplyThreadHeaders(
            in_reply_to=self.in_reply_to,
            references=self.references,
        )
        return self


class MailSendCommandSchema(_StrictCommandSchema):
    """``mail.send`` 的严格版本化 JSON Schema。"""

    schema_version: Literal["mail_send.v1"]
    action: Literal["mail.send"]
    operation_id: UUID
    connection_id: UUID
    draft_id: UUID
    draft_version: int = Field(gt=0)
    message_date: datetime
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body_text: str
    thread_headers: ReplyThreadHeadersSchema | None

    @field_validator("message_date", mode="before")
    @classmethod
    def _validate_raw_rfc3339_message_date(cls, value: object) -> datetime:
        """在 Pydantic 解析前拒绝其宽松接受的非 RFC 3339 日期文本。

        共享解析器要求大写 ``T``、完整秒、最多六位小数秒，以及 ``Z`` 或范围
        合法且非 ``-00:00`` 的数字 offset；错误不回显邮件时间原始输入。

        Args:
            value: 从标准 JSON 解码得到的 ``message_date`` 原始值。

        Returns:
            通过 RFC 3339 词法约束并显式解析的 aware ``datetime``，供后续
            strict 类型校验消费。

        Raises:
            ValueError: 原始值不是严格 RFC 3339 datetime 字符串。
        """
        return _parse_strict_rfc3339_datetime(value)

    @model_validator(mode="after")
    def _validate_and_normalize_domain_contract(self) -> Self:
        """校验完整邮件不变量，并把规范化收件人写回待哈希 Schema。"""
        command = _mail_domain_from_schema(self)
        # 规范 JSON 必须使用与领域执行完全相同的地址，不能只在执行时才归一。
        self.to = command.to
        self.cc = command.cc
        self.bcc = command.bcc
        return self


class _CalendarCommandSchema(_StrictCommandSchema):
    """三种日历命令共享的完整期望状态字段。"""

    operation_id: UUID
    connection_id: UUID
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime | date
    ends_at: datetime | date
    timezone: str
    all_day: bool
    attendees: tuple[str, ...]
    notification_policy: NotificationPolicy

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def _validate_raw_calendar_time(cls, value: object) -> date | datetime:
        """只把规范 ISO 日期或严格 RFC 3339 datetime 映射为领域时间类型。

        原始字符串的词法形式决定返回 ``date`` 还是 aware ``datetime``；具体命令的
        ``all_day`` 标志随后由纯领域构造器检查，因而混合表示、错误配对和非正向区间
        仍由同一领域事实来源拒绝。直接传入 Python 日期对象不能绕过 JSON 线格式。

        Args:
            value: 从标准 JSON 解码得到的 ``starts_at`` 或 ``ends_at`` 原始值。

        Returns:
            固定宽度日期字符串对应的 ``date``，或严格 RFC 3339 对应的 aware
            ``datetime``。

        Raises:
            ValueError: 原始值不符合允许的日期/datetime 语法或不是有效日历值。
        """
        if not isinstance(value, str):
            # Pydantic v2 不会把 validator 内的 TypeError 包装成统一 ValidationError。
            raise ValueError(  # noqa: TRY004
                "calendar time must be an ISO date or RFC 3339 string"
            )
        if _ISO_DATE_PATTERN.fullmatch(value) is not None:
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("calendar date must be a valid ISO date") from exc
        return _parse_strict_rfc3339_datetime(value)


class CalendarCreateCommandSchema(_CalendarCommandSchema):
    """``calendar.create`` 的严格版本化 JSON Schema。"""

    schema_version: Literal["calendar_create.v1"]
    action: Literal["calendar.create"]
    client_event_id: str

    @model_validator(mode="after")
    def _validate_and_normalize_domain_contract(self) -> Self:
        """校验创建时间表示与稳定标识，并写回规范化参会人。"""
        command = _calendar_create_domain_from_schema(self)
        self.attendees = command.attendees
        return self


class CalendarUpdateCommandSchema(_CalendarCommandSchema):
    """``calendar.update`` 的严格版本化 JSON Schema。"""

    schema_version: Literal["calendar_update.v1"]
    action: Literal["calendar.update"]
    provider_event_id: str
    base_etag: str
    before_snapshot_id: UUID
    changed_fields: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_and_normalize_domain_contract(self) -> Self:
        """校验完整期望状态、ETag、快照与确定性差异字段。"""
        command = _calendar_update_domain_from_schema(self)
        self.attendees = command.attendees
        return self


class CalendarRestoreCommandSchema(_CalendarCommandSchema):
    """``calendar.restore`` 的严格版本化 JSON Schema。"""

    schema_version: Literal["calendar_restore.v1"]
    action: Literal["calendar.restore"]
    provider_event_id: str
    base_etag: str
    before_snapshot_id: UUID
    changed_fields: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_and_normalize_domain_contract(self) -> Self:
        """校验恢复目标、当前 ETag、快照与确定性差异字段。"""
        command = _calendar_restore_domain_from_schema(self)
        self.attendees = command.attendees
        return self


type TrustedCommandSchema = Annotated[
    MailSendCommandSchema
    | CalendarCreateCommandSchema
    | CalendarUpdateCommandSchema
    | CalendarRestoreCommandSchema,
    Field(discriminator="action"),
]

_COMMAND_ADAPTER: TypeAdapter[TrustedCommandSchema] = TypeAdapter(TrustedCommandSchema)

type TrustedCommand = MailSendCommand | CalendarCommand


def parse_trusted_command(payload: Mapping[str, object]) -> TrustedCommand:
    """严格验证 JSON 载荷并显式映射为不依赖 Pydantic 的冻结领域命令。

    Args:
        payload: 来自 API、数据库解密或队列恢复的标准 JSON 对象。

    Returns:
        四种已批准 M2 动作之一的不可变领域命令。

    Raises:
        TypeError: 载荷包含 tuple、UUID 等非标准 JSON Python 对象。
        ValueError: 载荷包含 NaN、Infinity 或违反领域不变量。
        TrustedCommandValidationError: 判别联合、字段类型或领域验证失败；错误不含输入。
    """
    schema = _validate_command_schema(payload)
    if isinstance(schema, MailSendCommandSchema):
        return _mail_domain_from_schema(schema)
    if isinstance(schema, CalendarCreateCommandSchema):
        return _calendar_create_domain_from_schema(schema)
    if isinstance(schema, CalendarUpdateCommandSchema):
        return _calendar_update_domain_from_schema(schema)
    if isinstance(schema, CalendarRestoreCommandSchema):
        return _calendar_restore_domain_from_schema(schema)
    raise TypeError("validated command schema has an unsupported type")


def canonical_command_json(payload: Mapping[str, object]) -> bytes:
    """返回完整可信命令的 UTF-8 规范 JSON 字节。

    规范化在严格验证和领域归一之后执行；键排序、紧凑分隔符和未转义 Unicode
    共同保证同一命令在不同进程中产生相同字节。动作名与 Schema 版本不会被排除。

    Args:
        payload: 待验证和规范化的标准 JSON 对象。

    Returns:
        已按 UTF-8 编码、可直接用于审批哈希的稳定 JSON 字节。

    Raises:
        TypeError: 载荷含非标准 JSON Python 对象。
        ValueError: 载荷含非有限数值或违反领域约束。
        TrustedCommandValidationError: Schema 验证失败；错误不含原始命令内容。
    """
    schema = _validate_command_schema(payload)
    normalized = schema.model_dump(mode="json")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def trusted_command_hash(payload: Mapping[str, object]) -> str:
    """计算完整规范可信命令 UTF-8 字节的 SHA-256 十六进制哈希。

    Args:
        payload: 待绑定审批的标准 JSON 命令对象。

    Returns:
        包含 action、schema_version 和完整规范载荷的 64 字符十六进制哈希。

    Raises:
        TypeError: 载荷含非标准 JSON Python 对象。
        ValueError: 载荷含非有限数值或违反领域约束。
        TrustedCommandValidationError: Schema 验证失败；错误不含原始命令内容。
    """
    canonical = canonical_command_json(payload)
    return hashlib.sha256(canonical).hexdigest()


def _validate_command_schema(payload: Mapping[str, object]) -> TrustedCommandSchema:
    """验证严格判别联合，并把 Pydantic 细节替换为稳定脱敏应用错误。"""
    standard_json = _copy_standard_json(payload)
    encoded = json.dumps(
        standard_json,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        return _COMMAND_ADAPTER.validate_json(encoded, strict=True)
    except ValidationError:
        # 离开 except 后再抛出，确保新错误既没有 cause，也没有隐式 context 链。
        pass
    raise TrustedCommandValidationError


def _copy_standard_json(
    value: object,
    *,
    active_container_ids: set[int] | None = None,
) -> object:
    """递归复制标准 JSON 值，并拒绝会产生隐式线格式转换的 Python 对象。

    只接受 ``dict/Mapping``、``list``、字符串、精确 bool/int/float 和 ``None``。
    tuple、UUID、datetime、集合或自定义对象必须由调用方先显式序列化，防止同一
    Python 值在不同入口走出不同的规范字节。容器 ID 只在当前递归路径中保留，
    因而会拒绝直接或间接循环，但允许同一非循环子对象被多个字段安全共享。

    Args:
        value: 当前待复制的 Python 值。
        active_container_ids: 当前递归路径中的 Mapping/list 对象 ID；顶层调用省略。

    Returns:
        仅由标准 JSON Python 类型组成且不与输入共享容器的副本。

    Raises:
        ValueError: 数字非有限，或容器对象图包含直接或间接循环。
        TrustedCommandValidationError: 字符串值或对象键包含 Unicode 代理码位。
        TypeError: 出现非标准 JSON 值或非字符串对象键。
    """
    active_ids = set() if active_container_ids is None else active_container_ids
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        _validate_unicode_scalar_string(value)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("trusted command JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_ids:
            raise ValueError("trusted command JSON containers must not contain cycles")
        active_ids.add(container_id)
        try:
            copied: dict[str, object] = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise TypeError("trusted command JSON object keys must be strings")
                _validate_unicode_scalar_string(key)
                copied[key] = _copy_standard_json(
                    child,
                    active_container_ids=active_ids,
                )
            return copied
        finally:
            active_ids.remove(container_id)
    if type(value) is list:
        container_id = id(value)
        if container_id in active_ids:
            raise ValueError("trusted command JSON containers must not contain cycles")
        active_ids.add(container_id)
        try:
            return [
                _copy_standard_json(child, active_container_ids=active_ids)
                for child in value
            ]
        finally:
            active_ids.remove(container_id)
    raise TypeError("trusted command payload must contain only standard JSON values")


def _validate_unicode_scalar_string(value: str) -> None:
    """拒绝无法按 UTF-8 无损编码的 Unicode 代理码位。

    Python ``str`` 可以单独保存 U+D800..U+DFFF，但这些码位不是 Unicode scalar
    value，若进入 ``ensure_ascii=False`` JSON 会在编码阶段抛出携带完整载荷的
    ``UnicodeEncodeError``。可信命令必须在复制边界提前以固定应用错误拒绝。

    Args:
        value: 已确认是精确 ``str`` 类型的 JSON 字符串值或对象键。

    Raises:
        TrustedCommandValidationError: 字符串包含任一高位或低位代理码位。
    """
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise TrustedCommandValidationError


def _mail_domain_from_schema(schema: MailSendCommandSchema) -> MailSendCommand:
    """把已验证邮件 Schema 显式收窄为纯领域命令。"""
    thread_headers = (
        ReplyThreadHeaders(
            in_reply_to=schema.thread_headers.in_reply_to,
            references=schema.thread_headers.references,
        )
        if schema.thread_headers is not None
        else None
    )
    return MailSendCommand(
        schema_version=schema.schema_version,
        action=schema.action,
        operation_id=schema.operation_id,
        connection_id=schema.connection_id,
        draft_id=schema.draft_id,
        draft_version=schema.draft_version,
        message_date=schema.message_date,
        mode=schema.mode,
        source_thread_id=schema.source_thread_id,
        source_message_id=schema.source_message_id,
        to=schema.to,
        cc=schema.cc,
        bcc=schema.bcc,
        subject=schema.subject,
        body_text=schema.body_text,
        thread_headers=thread_headers,
    )


def _calendar_create_domain_from_schema(
    schema: CalendarCreateCommandSchema,
) -> CalendarCreateCommand:
    """把已验证创建 Schema 显式收窄为纯领域命令。"""
    return CalendarCreateCommand(
        schema_version=schema.schema_version,
        action=schema.action,
        operation_id=schema.operation_id,
        connection_id=schema.connection_id,
        calendar_id=schema.calendar_id,
        title=schema.title,
        description=schema.description,
        location=schema.location,
        starts_at=schema.starts_at,
        ends_at=schema.ends_at,
        timezone=schema.timezone,
        all_day=schema.all_day,
        attendees=schema.attendees,
        notification_policy=schema.notification_policy,
        client_event_id=schema.client_event_id,
    )


def _calendar_update_domain_from_schema(
    schema: CalendarUpdateCommandSchema,
) -> CalendarUpdateCommand:
    """把已验证修改 Schema 显式收窄为纯领域命令。"""
    return CalendarUpdateCommand(
        schema_version=schema.schema_version,
        action=schema.action,
        operation_id=schema.operation_id,
        connection_id=schema.connection_id,
        calendar_id=schema.calendar_id,
        title=schema.title,
        description=schema.description,
        location=schema.location,
        starts_at=schema.starts_at,
        ends_at=schema.ends_at,
        timezone=schema.timezone,
        all_day=schema.all_day,
        attendees=schema.attendees,
        notification_policy=schema.notification_policy,
        provider_event_id=schema.provider_event_id,
        base_etag=schema.base_etag,
        before_snapshot_id=schema.before_snapshot_id,
        changed_fields=schema.changed_fields,
    )


def _calendar_restore_domain_from_schema(
    schema: CalendarRestoreCommandSchema,
) -> CalendarRestoreCommand:
    """把已验证恢复 Schema 显式收窄为纯领域命令。"""
    return CalendarRestoreCommand(
        schema_version=schema.schema_version,
        action=schema.action,
        operation_id=schema.operation_id,
        connection_id=schema.connection_id,
        calendar_id=schema.calendar_id,
        title=schema.title,
        description=schema.description,
        location=schema.location,
        starts_at=schema.starts_at,
        ends_at=schema.ends_at,
        timezone=schema.timezone,
        all_day=schema.all_day,
        attendees=schema.attendees,
        notification_policy=schema.notification_policy,
        provider_event_id=schema.provider_event_id,
        base_etag=schema.base_etag,
        before_snapshot_id=schema.before_snapshot_id,
        changed_fields=schema.changed_fields,
    )


__all__ = [
    "CalendarCommand",
    "CalendarCreateCommandSchema",
    "CalendarRestoreCommandSchema",
    "CalendarUpdateCommandSchema",
    "MailSendCommandSchema",
    "ReplyThreadHeadersSchema",
    "TrustedCommand",
    "TrustedCommandSchema",
    "TrustedCommandValidationError",
    "canonical_command_json",
    "parse_trusted_command",
    "trusted_command_hash",
]
