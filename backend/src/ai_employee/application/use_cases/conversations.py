"""定义 M1 对话意图的安全范围说明。"""

UNSUPPORTED_RESPONSE = "当前 M1 仅支持生成或查看每日简报，不会执行外部写操作或通用规划。"


def unsupported_response() -> str:
    """返回不调用模型、工具或外部提供商的固定边界回复。"""
    return UNSUPPORTED_RESPONSE
