"""对话 worker 的安全回复辅助函数。"""

from ai_employee.application.use_cases.conversations import unsupported_response


def m1_boundary_reply() -> str:
    """对不支持写入或规划意图返回固定 M1 边界内容。"""
    return unsupported_response()
