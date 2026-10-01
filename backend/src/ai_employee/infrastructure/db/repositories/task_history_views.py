"""任务历史只读投影：固定八列、精确双向keyset与请求级一致性快照。"""

from typing import Literal

from sqlalchemy import ColumnElement, func, literal, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.task_history import (
    BACKGROUND_KINDS,
    BUSINESS_KINDS,
    HistoryCursorState,
    HistoryDirection,
    HistoryKey,
    HistoryReadPage,
    TaskHistoryFilters,
    TaskHistoryItem,
    history_category,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


def _user_dates(filters: TaskHistoryFilters) -> list[ColumnElement[bool]]:
    """构造所有查询共同的用户/UTC半开区间约束，计数不得复用列表种类状态。"""
    predicates = [TaskRunModel.user_id == filters.user_id]
    if filters.created_from is not None:
        predicates.append(TaskRunModel.created_at >= filters.created_from)
    if filters.created_before is not None:
        predicates.append(TaskRunModel.created_at < filters.created_before)
    return predicates


def _list_predicates(filters: TaskHistoryFilters) -> list[ColumnElement[bool]]:
    """只使用固定列比较扩展列表筛选；other和后台都包含未知旧任务类型。"""
    predicates = _user_dates(filters)
    if filters.scope == "business":
        predicates.append(TaskRunModel.kind.in_(BUSINESS_KINDS))
    elif filters.scope == "background":
        predicates.append(TaskRunModel.kind.not_in(BUSINESS_KINDS))
    if filters.kind == "other":
        predicates.append(TaskRunModel.kind.not_in(BUSINESS_KINDS | BACKGROUND_KINDS))
    elif filters.kind is not None:
        predicates.append(TaskRunModel.kind == filters.kind)
    if filters.status is not None:
        predicates.append(TaskRunModel.status == filters.status.value)
    return predicates


def _compare_key(
    key: HistoryKey, direction: HistoryDirection | Literal["upper"]
) -> ColumnElement[bool]:
    """在数据库以完整微秒和UUID比较，不通过秒级时间戳或字符串降精度。"""
    columns = tuple_(TaskRunModel.created_at, TaskRunModel.id)
    boundary = tuple_(literal(key.created_at), literal(key.task_id))
    if direction == "older":
        return columns < boundary
    if direction == "newer":
        return columns > boundary
    return columns <= boundary


async def _exists(
    session: AsyncSession,
    predicates: list[ColumnElement[bool]],
    key: HistoryKey,
    direction: HistoryDirection,
) -> bool:
    """在同一快照判断方向，所有EXISTS继承用户、列表条件及upper。"""
    statement = select(
        select(TaskRunModel.id).where(*predicates, _compare_key(key, direction)).exists()
    )
    return bool(await session.scalar(statement))


class SqlAlchemyTaskHistoryReader:
    """只持有受管会话工厂；不引用旧详情Store或执行/供应商端口。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存工厂，每次读取独立创建并关闭短只读事务。"""
        self._session_factory = session_factory

    async def read_page(
        self, *, filters: TaskHistoryFilters, cursor: HistoryCursorState | None
    ) -> HistoryReadPage:
        """从一个REPEATABLE READ READ ONLY快照读取摘要、计数和双向证据。

        Args:
            filters: 规范化用户隔离筛选。
            cursor: 已验证游标；其锚点行允许被删除，分页不回查该行。
        Returns:
            倒序安全摘要；空窗口仅返回有反向记录支持的原锚点。

        快照不跨请求存活，不锁业务行。创建顺序上界只限制列表，后台failed
        聚合仍计算当前用户日期范围内全部非业务任务，不作“尚未处理”推断。
        """
        async with self._session_factory.begin() as session:
            # 必须是事务第一条语句，禁止摘要、方向与计数混用并发提交后的新快照。
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            predicates = _list_predicates(filters)
            upper = cursor.upper if cursor is not None else None
            if upper is not None:
                predicates.append(_compare_key(upper, "upper"))
            columns = (
                TaskRunModel.id,
                TaskRunModel.kind,
                TaskRunModel.status,
                TaskRunModel.created_at,
                TaskRunModel.started_at,
                TaskRunModel.finished_at,
                TaskRunModel.error_code,
                TaskRunModel.retry_of_task_id,
            )
            statement = select(*columns).where(*predicates)
            newer = cursor is not None and cursor.direction == "newer"
            if cursor is not None:
                statement = statement.where(_compare_key(cursor.anchor, cursor.direction))
            order = (TaskRunModel.created_at, TaskRunModel.id)
            statement = statement.order_by(
                *(column.asc() if newer else column.desc() for column in order)
            ).limit(filters.limit + 1)
            rows = list((await session.execute(statement)).all()[: filters.limit])
            if newer:
                rows.reverse()
            items = tuple(
                TaskHistoryItem(
                    row.id,
                    row.kind,
                    history_category(row.kind),
                    TaskStatus(row.status),
                    row.created_at,
                    row.started_at,
                    row.finished_at,
                    row.error_code,
                    row.retry_of_task_id,
                )
                for row in rows
            )
            if upper is None and items:
                upper = HistoryKey(items[0].created_at, items[0].id)
                predicates.append(_compare_key(upper, "upper"))
            next_anchor = previous_anchor = None
            if items:
                first = HistoryKey(items[0].created_at, items[0].id)
                last = HistoryKey(items[-1].created_at, items[-1].id)
                if await _exists(session, predicates, last, "older"):
                    next_anchor = last
                if await _exists(session, predicates, first, "newer"):
                    previous_anchor = first
            elif cursor is not None:
                # 原方向已经为空，绝不返回相同方向游标形成空页自动循环。
                reverse: HistoryDirection = "newer" if cursor.direction == "older" else "older"
                if await _exists(session, predicates, cursor.anchor, reverse):
                    if reverse == "newer":
                        previous_anchor = cursor.anchor
                    else:
                        next_anchor = cursor.anchor
            count = await session.scalar(
                select(func.count())
                .select_from(TaskRunModel)
                .where(
                    *_user_dates(filters),
                    TaskRunModel.kind.not_in(BUSINESS_KINDS),
                    TaskRunModel.status == TaskStatus.FAILED.value,
                )
            )
            return HistoryReadPage(items, upper, next_anchor, previous_anchor, count or 0)
