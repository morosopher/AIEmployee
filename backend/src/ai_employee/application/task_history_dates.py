"""历史查询的 IANA 民用日与严格 UTC 编码；不依赖历史 DTO，避免循环导入。"""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATE_ERROR = "task_history_date_invalid"
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def local_day_start(day: date, timezone: str) -> datetime:
    """计算本地民用日的第一个有效 UTC 瞬间。

    Args:
        day: 不携带时区的目标民用日期。
        timezone: 显式 IANA 时区名称，不允许本地时区推断。

    Returns:
        重叠午夜取最早有效候选；跳跃取首次达到目标日期的瞬间。
        整日跳过时返回下一存在日的起点，因此该日期的半开区间为空。

    Raises:
        ValueError: 时区无效或日期转换越界，错误文本固定且不含输入。
    """
    try:
        zone = ZoneInfo(timezone)
        midnight = datetime.combine(day, time.min)
        candidates = sorted(
            midnight.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)
        )
        valid = [
            candidate
            for candidate in candidates
            if candidate.astimezone(zone).replace(tzinfo=None) == midnight
        ]
        if valid:
            return valid[0]
        # 两个 fold 对跳跃给出转换前后偏移，夹住实际跳跃瞬间；只在这段
        # 有界区间二分，保留数据库微秒精度，不按分钟扫描或假设一天 24 小时。
        lower, upper = candidates
        while upper - lower > timedelta(microseconds=1):
            middle = lower + (upper - lower) // 2
            if middle.astimezone(zone).date() >= day:
                upper = middle
            else:
                lower = middle
        return upper
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ValueError(_DATE_ERROR) from exc


def canonical_utc_timestamp(value: datetime) -> str:
    """把带时区时间规范化为固定六位微秒、Z 后缀的 UTC 文本。

    Args:
        value: 带时区且可转换为 UTC 的瞬间。

    Returns:
        四位年和六位微秒组成的唯一时间表示，保留分页比较精度。

    Raises:
        ValueError: naive 时间或转换溢出；不在异常消息中回显输入。
    """
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(_DATE_ERROR)
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError(_DATE_ERROR) from exc


def parse_history_timestamp(value: str) -> datetime:
    """严格解码历史游标时间，拒绝宽松 ISO 表示及非法日期。

    Args:
        value: 六位微秒、Z 后缀的规范 UTC 字符串。

    Returns:
        保留全部微秒且 tzinfo 为 UTC 的时间。

    Raises:
        ValueError: 格式或日期无效，始终使用无载荷固定错误。
    """
    try:
        if not _TIMESTAMP.fullmatch(value):
            raise ValueError(_DATE_ERROR)
        return datetime.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(_DATE_ERROR) from exc
