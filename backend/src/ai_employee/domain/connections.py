"""定义外部只读数据连接的稳定领域状态。"""

from enum import StrEnum


class ConnectionStatus(StrEnum):
    """描述 OAuth 数据连接对后续同步的可用性。

    状态值会持久化并被 API 返回，因此使用稳定英文字符串；只有 ``CONNECTED``
    允许正常增量同步，断开或授权失效必须阻止未来同步任务继续读取供应商数据。
    """

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    DISCONNECTED = "disconnected"
