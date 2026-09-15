"""复用 Taskiq Stream 协议，以 Redis 原子重领恢复崩溃进程遗留的 pending。

taskiq-redis 的默认监听器使用无过期时间的辅助分布式锁，持锁进程被 SIGKILL 后会永久
阻止重领。XAUTOCLAIM 自身已原子更新 owner 与 idle；业务执行的权限、租约和幂等仍由
PostgreSQL 负责，因此这里只覆写监听循环，不增加第二份执行权威或新的运行开关。
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from redis.typing import KeyT, StreamIdT
from taskiq import AckableMessage
from taskiq_redis import RedisStreamBroker


class RedisTaskStreamBroker(RedisStreamBroker):
    """保留既有发布、注册、编码和 ACK，以独立游标遍历 pending 且不依赖辅助锁。

    多个 Worker 可以并发调用 XAUTOCLAIM；Redis 会把每条成功重领的消息的 idle 归零，
    未达到 idle_timeout 的消息不会被下一位 Worker 立即再次接管。消息仍按至少一次语义
    处理，真正的重复执行和过期 owner 必须继续经过 TaskRun/ToolExecution 数据库防线。
    """

    async def listen(self) -> AsyncGenerator[AckableMessage, None]:
        """读取新消息并重领自然到期的原 pending，只有原 ACK 回调确认队列消息。

        Yields:
            原有 Taskiq 编码消息及其精确 Stream entry 的 XACK 回调。

        Raises:
            RedisError: Redis 连接或命令失败原样交由 Taskiq 管理进程处理。

        不创建、续期、删除或依赖旧版本的 autoclaim 辅助锁。旧孤儿锁不影响恢复，也不
        需要启动时遍历或清理 Redis。循环保持原 broker 的队列、consumer group、批量和
        等待参数；消息正文不进入日志。每个 stream 的扫描位置只属于当前 listener，
        重启时可从头恢复，不成为业务事实或执行权限。
        """
        pending_cursors: dict[str, StreamIdT] = {}
        async with Redis(connection_pool=self.connection_pool) as client:
            while True:
                streams: dict[KeyT, StreamIdT] = {self.queue_name: ">"}
                for name, position in self.additional_streams.items():
                    streams[name] = position
                fetched = await client.xreadgroup(
                    self.consumer_group_name,
                    self.consumer_name,
                    streams,
                    block=self.block,
                    noack=False,
                    count=self.count,
                )
                for stream, entries in fetched:
                    for message_id, message in entries:
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=message_id, queue_name=stream),
                        )
                for stream in (self.queue_name, *self.additional_streams):
                    pending = await client.xautoclaim(
                        name=stream,
                        groupname=self.consumer_group_name,
                        consumername=self.consumer_name,
                        min_idle_time=self.idle_timeout,
                        start_id=pending_cursors.get(stream, "0-0"),
                        count=self.unacknowledged_batch_size,
                    )
                    # COUNT 限制领取量，Redis 最多只扫描其十倍 PEL 项；空消息页也可能尚未
                    # 扫完，必须推进返回游标。0-0 表示本轮结束，下轮从头检查后来到期的项。
                    # 在 yield 前保存该 stream 的位置，附加 stream 不能继承别的扫描进度。
                    pending_cursors[stream] = pending[0]
                    for message_id, message in pending[1]:
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=message_id, queue_name=stream),
                        )
