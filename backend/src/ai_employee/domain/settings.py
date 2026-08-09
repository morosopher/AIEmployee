"""定义用户可安全修改的简报、工作时间与会议缓冲设置值对象。"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCALE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2}$")
_WORKING_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


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
    parts = value.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.upper() if len(part) == 2 and part.isalpha() else part.title())
    return "-".join(normalized)


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


def validate_meeting_buffer(value: object) -> int:
    """验证会议前后共同使用的分钟缓冲。

    Args:
        value: 设置/API 边界收到的动态值。

    Returns:
        可安全构造 ``timedelta`` 的普通整数分钟数。

    Raises:
        TypeError: 输入不是普通整数，尤其拒绝 ``bool``。
        ValueError: 输入超出 M2 允许的 0～120 分钟。
    """
    if type(value) is not int:
        raise TypeError("meeting buffer must be an integer")
    if not 0 <= value <= 120:
        raise ValueError("meeting buffer must be between 0 and 120 minutes")
    return value


@dataclass(frozen=True, slots=True, order=True)
class WorkingInterval:
    """表示同一用户本地日内一个不可变、严格正向工作区间。

    开始与结束只承载墙上 ``HH:MM``，不得含时区、秒或微秒。跨午夜区间在 M2 中必须
    拆到相邻两天，避免候选算法猜测日期归属。
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        """拒绝带时区、亚分钟精度、零长、反向或跨午夜区间。"""
        for value in (self.start, self.end):
            if not isinstance(value, time):
                raise TypeError("working interval endpoints must be time values")
            if value.tzinfo is not None or value.second != 0 or value.microsecond != 0:
                raise ValueError("working interval endpoints must be local HH:MM values")
        if self.end <= self.start:
            raise ValueError("working interval end must be after start")


@dataclass(frozen=True, slots=True)
class WeeklyWorkingHours:
    """保存星期一到星期日每天零个或多个不重叠工作区间。

    ``days`` 固定为七元素 tuple，索引与 :meth:`datetime.date.weekday` 一致。构造入口
    要求 JSON 映射完整覆盖七天；区间会按开始时间规范排序，但任何重叠、坏格式或未知
    星期名都会被拒绝，绝不通过合并或裁剪猜测用户意图。
    """

    days: tuple[tuple[WorkingInterval, ...], ...]

    def __post_init__(self) -> None:
        """验证七天容器、元素类型、排序与不重叠不变量。"""
        if not isinstance(self.days, tuple) or len(self.days) != 7:
            raise ValueError("weekly working hours must contain exactly seven days")
        for intervals in self.days:
            if not isinstance(intervals, tuple) or any(
                not isinstance(interval, WorkingInterval) for interval in intervals
            ):
                raise TypeError("working hours days must contain WorkingInterval tuples")
            if tuple(sorted(intervals)) != intervals:
                raise ValueError("working intervals must be deterministically sorted")
            for previous, current in pairwise(intervals):
                if current.start < previous.end:
                    raise ValueError("working intervals must not overlap")

    @classmethod
    def from_mapping(cls, value: object) -> "WeeklyWorkingHours":
        """从设置 JSON 严格解析完整七天工作时间。

        Args:
            value: 预期键为英文星期名、值为 ``[[HH:MM, HH:MM], ...]`` 的映射。

        Returns:
            已冻结、排序且通过不重叠校验的周工作时间。

        Raises:
            TypeError: 根、每日容器或端点类型不符合声明。
            ValueError: 缺日、未知键、时间格式、方向或重叠不合法。
        """
        if not isinstance(value, Mapping):
            raise TypeError("working hours must be a mapping")
        if set(value) != set(WEEKDAY_NAMES) or any(type(key) is not str for key in value):
            raise ValueError("working hours must contain exactly the seven weekday names")
        parsed_days: list[tuple[WorkingInterval, ...]] = []
        for name in WEEKDAY_NAMES:
            raw_intervals = value[name]
            if not isinstance(raw_intervals, Sequence) or isinstance(
                raw_intervals, (str, bytes, bytearray)
            ):
                raise TypeError("working hours day must be a sequence")
            intervals: list[WorkingInterval] = []
            for raw_interval in raw_intervals:
                if not isinstance(raw_interval, Sequence) or isinstance(
                    raw_interval, (str, bytes, bytearray)
                ):
                    raise TypeError("working interval must be a two-item sequence")
                if len(raw_interval) != 2:
                    raise ValueError("working interval must contain start and end")
                start, end = raw_interval
                intervals.append(
                    WorkingInterval(
                        _parse_working_time(start),
                        _parse_working_time(end),
                    )
                )
            parsed_days.append(tuple(sorted(intervals)))
        return cls(tuple(parsed_days))

    @classmethod
    def default(cls) -> "WeeklyWorkingHours":
        """返回 M2 迁移默认的周一至周五 09:00～18:00。"""
        return cls.from_mapping(
            {
                name: [["09:00", "18:00"]] if index < 5 else []
                for index, name in enumerate(WEEKDAY_NAMES)
            }
        )

    def intervals_for(self, weekday: int) -> tuple[WorkingInterval, ...]:
        """按 ``date.weekday()`` 索引返回不可变日区间。

        Raises:
            TypeError: ``weekday`` 不是普通整数。
            ValueError: ``weekday`` 不在 0～6。
        """
        if type(weekday) is not int:
            raise TypeError("weekday must be an integer")
        if not 0 <= weekday <= 6:
            raise ValueError("weekday must be between 0 and 6")
        return self.days[weekday]

    def to_mapping(self) -> dict[str, list[list[str]]]:
        """返回可写入 JSONB 的规范七天映射副本。"""
        return {
            name: [
                [interval.start.strftime("%H:%M"), interval.end.strftime("%H:%M")]
                for interval in self.days[index]
            ]
            for index, name in enumerate(WEEKDAY_NAMES)
        }


def _parse_working_time(value: object) -> time:
    """严格解析零填充 ``HH:MM``，不接受自动补零或秒字段。"""
    if type(value) is not str or _WORKING_TIME.fullmatch(value) is None:
        raise ValueError("working interval endpoint must be HH:MM")
    return time.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class UserSettings:
    """设置视图的稳定领域表示，不含认证或隐私字段。"""
    timezone: str
    locale: str
    brief_time: time
    email_body_retention_days: int
    source_metadata_retention_days: int
    workspace_history_retention_days: int
    working_hours: WeeklyWorkingHours = field(default_factory=WeeklyWorkingHours.default)
    meeting_buffer_minutes: int = 10
