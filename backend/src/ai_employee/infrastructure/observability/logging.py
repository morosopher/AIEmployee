"""配置字段固定、输入已脱敏的 JSON 结构化日志。"""

import hashlib
import json
import logging
import traceback

from ai_employee.infrastructure.observability.redaction import redact_value


def diagnostic_user_digest(user_id: str) -> str:
    """生成不可逆用户诊断维度，避免原始 UUID 或邮箱进入日志。

    Args:
        user_id: 已认证用户的内部 UUID 字符串。

    Returns:
        固定长度 SHA-256 摘要，仅供同进程诊断关联。
    """
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def configure_json_logging(secret_patterns: tuple[str, ...] = ()) -> None:
    """安装仅输出脱敏 JSON 的根日志 handler，重复调用不会叠加 handler。"""
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter(secret_patterns))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class _JsonFormatter(logging.Formatter):
    """把标准日志属性与允许的 extras 收窄为稳定 JSON 信封。"""

    def __init__(self, secret_patterns: tuple[str, ...]) -> None:
        super().__init__()
        self._secret_patterns = secret_patterns

    def format(self, record: logging.LogRecord) -> str:
        """省略 Python 异常原文，避免供应商错误文本越过脱敏边界。"""
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname.lower(),
            # 标准 logging 的 message 可能是异常插值或任意调用方字符串；使用 logger 名称
            # 作为稳定事件，而非读取 ``getMessage`` 或 exception 文本。
            "event": record.name if record.name.startswith("ai_employee.") else "application.log",
            "trace_id": getattr(record, "trace_id", None),
            "task_id": getattr(record, "task_id", None),
            "step_id": getattr(record, "step_id", None),
            "provider": getattr(record, "provider", None),
            "error_code": getattr(record, "error_code", None),
        }
        if record.exc_info is not None:
            # 只记录代码位置，刻意不调用 formatException：异常消息和 locals 可能包含请求正文、
            # Cookie、授权头或供应商响应。函数名与行号足以让 trace_id 关联到受控源码版本。
            payload["stack"] = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(redact_value(payload, secret_patterns=self._secret_patterns), ensure_ascii=False)
