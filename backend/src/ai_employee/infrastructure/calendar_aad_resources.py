"""保持 Task27C 同步 I/O 与窄备份子进程的结果所有权，不让重复取消穿透收尾。

这里只等待调用方已经创建且独占的 Task，不创建工作、重试或维护 authority；provider、
普通业务协程和外部拥有的任务不使用此边界。资源的释放与文件补偿仍由具体调用方负责。
"""

import asyncio


async def await_calendar_aad_resource[T](task: asyncio.Task[T]) -> T:
    """持续保护本次拥有的工作到终态，再传播首次取消或返回其正常结果。

    Args:
        task: 同一事件循环中由调用方独占的 Task；调用方保留引用以接回资源所有权。

    Returns:
        未收到取消时的原始类型化结果，不复制或重新执行底层工作。

    Raises:
        asyncio.CancelledError: 等待期间的首次取消；其后取消不会取消底层 Task。
        BaseException: 未收到取消时，原 Task 的失败保持原样传播。

    同步线程不会随 asyncio Task 的取消停止，因此每一次等待都必须 shield。Task 达到
    终态后先消费结果/异常；若随后传播取消，调用方仍须从该 Task 取回 lease、Path 或发布
    标志并完成补偿。这里不调用 uncancel，嵌套 timeout 仍拥有各自的取消计数与转换语义。
    """
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:
            # 正常工作失败由下方 result() 原样还原；错误 Task 不会留下未消费的异常。
            if not task.done():
                raise
            break
    try:
        return task.result()
    finally:
        if cancellation is not None:
            raise cancellation
