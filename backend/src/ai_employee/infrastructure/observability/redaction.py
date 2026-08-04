"""提供进入日志、指标和追踪前的统一敏感数据删除能力。"""

import re
from collections.abc import Mapping, Sequence

type RedactedValue = str | int | float | bool | None | dict[str, "RedactedValue"] | list["RedactedValue"]

# 字段名匹配故意采用保守的子串规则：可观测性宁可少记录，也绝不能把凭据或正文写出边界。
_SENSITIVE_FIELD_TERMS = frozenset(
    {
        "authorization",
        "cookie",
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "body",
        "prompt",
        "content_markdown",
        "ciphertext",
        "nonce",
    }
)

# 字符串没有字段名时仍可能来自异常文本或插值日志；只允许记录不带敏感标签的诊断值。
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:authorization|cookie|token|api[_ -]?key|secret|password|body|prompt)\s*[:=]",
    re.IGNORECASE,
)


def redact_value(
    value: object, *, secret_patterns: Sequence[str] = ()
) -> RedactedValue:
    """递归移除不允许进入可观测性管道的字段和值。

    Args:
        value: 准备写入日志、span 属性或诊断元数据的外部值。
        secret_patterns: 配置提供的字段名正则；非法正则会被忽略，避免诊断路径因配置
            错误而泄露未处理数据。

    Returns:
        只包含基础 JSON 标量或同类安全容器的副本。敏感字段直接省略，不使用原文替换符，
        以免攻击者借长度或格式反推秘密。
    """
    compiled_patterns = _compile_patterns(secret_patterns)
    return _redact(value, compiled_patterns)


def _compile_patterns(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """编译可选字段名规则，并在错误配置时采取安全的忽略策略。"""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            # 配置属于运维输入；记录该配置本身会造成循环依赖，因此只安全忽略。
            continue
    return tuple(compiled)


def _redact(value: object, patterns: tuple[re.Pattern[str], ...]) -> RedactedValue:
    """将未知输入缩窄成不会触发对象序列化的安全 JSON 值。"""
    if isinstance(value, str):
        # 运维正则同时约束字段名和值，防止异常文本绕过结构化 payload 的键过滤。
        if _SENSITIVE_VALUE_PATTERN.search(value) is not None or any(
            pattern.search(value) is not None for pattern in patterns
        ):
            return "[redacted]"
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, RedactedValue] = {}
        for key, nested in value.items():
            normalized_key = str(key)
            if _is_sensitive_field(normalized_key, patterns):
                continue
            result[normalized_key] = _redact(nested, patterns)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_redact(item, patterns) for item in value]
    # 禁止调用未知对象的 __str__，其实现可能包含消息正文、凭据或供应商响应。
    return "[unsupported]"


def _is_sensitive_field(key: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """判断字段名是否属于固定或运维配置的敏感集合。"""
    normalized = key.casefold().replace("-", "_")
    return any(term in normalized for term in _SENSITIVE_FIELD_TERMS) or any(
        pattern.search(key) is not None for pattern in patterns
    )
