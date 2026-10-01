"""只读历史列表用例：规范筛选、验证游标并签发有数据库方向证据的分页入口。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.task_history import (
    HistoryCursorState,
    HistoryReadPage,
    TaskHistoryFilters,
    TaskHistoryPage,
    TaskHistoryQuery,
    history_filters_hash,
    normalize_history_filters,
)
from ai_employee.application.use_cases.auth import Clock


class TaskHistoryReader(Protocol):
    """用户隔离的只读端口；实现必须在一个短快照中读取摘要、方向和计数。"""

    async def read_page(
        self, *, filters: TaskHistoryFilters, cursor: HistoryCursorState | None
    ) -> HistoryReadPage:
        """读取已规范化筛选；cursor已经校验，不代表身份授权。"""
        ...


class HistoryCursorCodec(Protocol):
    """供应商无关的专用分页签名边界，不承载业务执行权限。"""

    def encode(self, state: HistoryCursorState) -> str:
        """签发固定分页元数据；密钥错误保持原始安全错误。"""
        ...

    def decode(
        self, token: str, *, filters: TaskHistoryFilters, now: datetime
    ) -> HistoryCursorState:
        """验证用户、筛选和时效；非法输入抛出无载荷TaskHistoryCursorError。"""
        ...


class ListTaskHistoryUseCase:
    """协调一次只读列表请求，绝不调度任务或访问外部供应商。"""

    def __init__(
        self,
        reader: TaskHistoryReader,
        codec_factory: Callable[[], HistoryCursorCodec],
        clock: Clock,
    ) -> None:
        """保存只读端口、按需密钥工厂和时钟；不在构造时读取Secret。

        Args:
            reader: 仅返回安全摘要和方向证据的读取端口。
            codec_factory: 同步工厂，可能读取文件，实际调用必须在线程中执行。
            clock: 每次请求只读取一次，用于验证、签发和响应时间。
        """
        self._reader = reader
        self._codec_factory = codec_factory
        self._clock = clock

    async def execute(
        self, *, user_id: UUID, timezone: str, query: TaskHistoryQuery
    ) -> TaskHistoryPage:
        """规范筛选并完成一次短读取，只为仍有方向记录的锚点签发游标。

        Args:
            user_id: 来自认证的用户标识，不从游标推导。
            timezone: 用户当前配置的IANA时区。
            query: 原始筛选和可选游标。
        Returns:
            不含任务载荷的不可变列表响应；空页也可能有恢复方向。
        Raises:
            TaskHistoryFilterError: 筛选格式或组合非法，读取前拒绝。
            TaskHistoryCursorError: 游标非法、过期或绑定不匹配，读取前拒绝。
        """
        filters = normalize_history_filters(user_id=user_id, timezone=timezone, query=query)
        now = self._clock.now()
        codec = None
        cursor = None
        if query.cursor is not None:
            codec = await asyncio.to_thread(self._codec_factory)
            cursor = codec.decode(query.cursor, filters=filters, now=now)
        page = await self._reader.read_page(filters=filters, cursor=cursor)
        next_cursor = previous_cursor = None
        if page.next_anchor is not None or page.previous_anchor is not None:
            if page.upper is None:
                raise RuntimeError("task_history_upper_missing")
            if codec is None:
                codec = await asyncio.to_thread(self._codec_factory)
            fingerprint = history_filters_hash(filters)
            if page.next_anchor is not None:
                next_cursor = codec.encode(
                    HistoryCursorState(
                        user_id, fingerprint, "older", page.upper, page.next_anchor, now
                    )
                )
            if page.previous_anchor is not None:
                previous_cursor = codec.encode(
                    HistoryCursorState(
                        user_id, fingerprint, "newer", page.upper, page.previous_anchor, now
                    )
                )
        return TaskHistoryPage(
            page.items,
            next_cursor,
            previous_cursor,
            now,
            filters.timezone,
            page.background_failed_count,
        )
