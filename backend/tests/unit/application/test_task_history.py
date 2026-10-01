"""通过无外部写端口的Fake验证历史用例、惰性密钥和统一时间。"""

import threading
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_employee.application.task_history import (
    HistoryCursorState,
    HistoryKey,
    HistoryReadPage,
    TaskHistoryCursorError,
    TaskHistoryFilters,
    TaskHistoryQuery,
)
from ai_employee.application.use_cases.task_history import ListTaskHistoryUseCase

NOW = datetime(2029, 1, 2, tzinfo=UTC)
KEY = HistoryKey(NOW, UUID(int=1))


class FakeClock:
    """固定时间且计数，防止验证和签发各读一次时钟。"""

    calls = 0

    def now(self) -> datetime:
        """返回确定的UTC时刻。"""
        self.calls += 1
        return NOW


class FakeReader:
    """仅提供读取能力，没有dispatcher或供应商依赖。"""

    def __init__(self, page: HistoryReadPage) -> None:
        """保存返回页和读取计数。"""
        self.page = page
        self.calls = 0

    async def read_page(
        self, *, filters: TaskHistoryFilters, cursor: HistoryCursorState | None
    ) -> HistoryReadPage:
        """统计调用并返回固定结果。"""
        self.calls += 1
        return self.page


class FakeCodec:
    """保存签发元数据，并提供无效游标确定性拒绝。"""

    def __init__(self) -> None:
        """初始化签发记录。"""
        self.states: list[HistoryCursorState] = []

    def encode(self, state: HistoryCursorState) -> str:
        """仅返回方向标签，不产生真实秘密或游标。"""
        self.states.append(state)
        return state.direction

    def decode(
        self, token: str, *, filters: TaskHistoryFilters, now: datetime
    ) -> HistoryCursorState:
        """无效令牌直接拒绝，否则记录收到的时钟。"""
        if token == "invalid":
            raise TaskHistoryCursorError()
        assert now == NOW
        return HistoryCursorState(filters.user_id, "synthetic", "older", KEY, KEY, now)


@pytest.mark.parametrize("token", [None, "valid", "invalid"])
async def test_lazy_codec_and_invalid_cursor_before_reader(token: str | None) -> None:
    """无游标空页不读密钥；无效游标零查询；工厂在其他线程且最多一次。"""
    reader = FakeReader(HistoryReadPage((), None, None, None, 3))
    codec, clock = FakeCodec(), FakeClock()
    threads: list[int] = []

    def factory() -> FakeCodec:
        threads.append(threading.get_ident())
        return codec

    use_case = ListTaskHistoryUseCase(reader, factory, clock)
    if token == "invalid":
        with pytest.raises(TaskHistoryCursorError):
            await use_case.execute(
                user_id=UUID(int=10), timezone="UTC", query=TaskHistoryQuery(cursor=token)
            )
        assert reader.calls == 0
    else:
        page = await use_case.execute(
            user_id=UUID(int=10), timezone="UTC", query=TaskHistoryQuery(cursor=token)
        )
        assert page.background_failed_count == 3
        assert page.next_cursor is page.previous_cursor is None
        assert page.server_time == NOW
        assert reader.calls == 1
    assert len(threads) == (0 if token is None else 1)
    assert all(thread != threading.get_ident() for thread in threads)
    assert clock.calls == 1


@pytest.mark.parametrize("token", [None, "valid"])
async def test_empty_recovery_anchors_are_encoded_and_codec_reused(token: str | None) -> None:
    """空窗口方向由Reader的EXISTS证据决定，用例不凭空制造方向。"""
    reader = FakeReader(HistoryReadPage((), KEY, KEY, KEY, 0))
    codec, clock = FakeCodec(), FakeClock()
    calls: list[bool] = []

    def factory() -> FakeCodec:
        calls.append(True)
        return codec

    result = await ListTaskHistoryUseCase(reader, factory, clock).execute(
        user_id=UUID(int=10), timezone="UTC", query=TaskHistoryQuery(cursor=token)
    )
    assert result.next_cursor == "older"
    assert result.previous_cursor == "newer"
    assert len(calls) == clock.calls == 1
    assert [state.issued_at for state in codec.states] == [NOW, NOW]
    assert all(state.upper == KEY and state.anchor == KEY for state in codec.states)
