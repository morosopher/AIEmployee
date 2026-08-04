"""定义所有云模型请求前执行的本地文本脱敏规则。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

_BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[^\s]+")),
    (
        "api_key",
        re.compile(r"(?i)\b(?:api[_-]?key\s*[=:]\s*[^\s]+|sk-[a-z0-9_-]{8,})"),
    ),
    ("one_time_code", re.compile(r"(?<!\d)\d{6}(?!\d)")),
)
_REDACTED_VALUE = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class ModelRedactionResult:
    """已脱敏文本及不泄露原始值的本地处理审计码。"""

    text: str
    reason_codes: tuple[str, ...]


def redact_for_model(
    text: str,
    *,
    configured_patterns: Sequence[str] = (),
) -> ModelRedactionResult:
    """使用内置与用户配置正则脱敏云模型输入。

    所有替换均在本地完成，返回值仅记录规则名称而不携带匹配内容，以便任务时间线
    报告发生过掩码处理而不重新暴露秘密。配置规则按输入顺序执行；无匹配规则不
    产生原因码，普通姓名和会议时间不会被通用秘密规则改变。

    Args:
        text: 准备发送至云模型的原始文本。
        configured_patterns: 用户配置的额外正则文本，按顺序应用。

    Returns:
        用固定占位符替换敏感片段后的文本与稳定原因码。

    Raises:
        re.error: 用户配置的正则语法非法。
    """
    redacted_text = text
    reason_codes: list[str] = []
    for reason_code, pattern in _BUILTIN_PATTERNS:
        redacted_text, replacement_count = pattern.subn(_REDACTED_VALUE, redacted_text)
        if replacement_count:
            reason_codes.append(reason_code)

    for index, pattern_text in enumerate(configured_patterns, start=1):
        pattern = re.compile(pattern_text)
        redacted_text, replacement_count = pattern.subn(_REDACTED_VALUE, redacted_text)
        if replacement_count:
            reason_codes.append(f"configured_pattern_{index}")
    return ModelRedactionResult(redacted_text, tuple(reason_codes))
