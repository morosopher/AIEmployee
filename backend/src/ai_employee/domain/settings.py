"""定义用户可安全修改的每日简报设置值对象。"""

import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCALE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2}$")


def validate_timezone(value: str) -> str:
    """验证 IANA 时区，禁止使用宿主机隐式时区。"""
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("timezone must be a valid IANA timezone") from error
    return value


def validate_locale(value: str) -> str:
    """验证有限 BCP47 风格语言标签，避免自由文本进入设置。"""
    if not _LOCALE.fullmatch(value) or not 2 <= len(value) <= 16:
        raise ValueError("locale must be a BCP47-style tag")
    return value


def validate_brief_time(value: str) -> time:
    """严格解析零填充的二十四小时 HH:MM。"""
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("brief_time must be HH:MM")
    return time.fromisoformat(value)


def validate_retention(value: int) -> int:
    """限制所有保留天数，防止错误值绕过数据库约束。"""
    if not 1 <= value <= 3650:
        raise ValueError("retention days must be between 1 and 3650")
    return value


@dataclass(frozen=True, slots=True)
class UserSettings:
    """设置视图的稳定领域表示，不含认证或隐私字段。"""
    timezone: str
    locale: str
    brief_time: time
    email_body_retention_days: int
    source_metadata_retention_days: int
    workspace_history_retention_days: int
