"""承载供应商 HTTP 与 SDK 适配器，隔离外部字段和网络边界。"""

from ai_employee.infrastructure.observability.logging import configure_http_client_logging

# 适配器也会被契约测试、CLI 或后台脚本直接导入，可能尚未经过 API/Worker composition
# root。导入 integrations 时先建立日志安全默认值，确保任何 HTTPX 请求都不会把供应商
# query、Authorization 或异常对象传播到宿主 logger；正式进程随后仍会重复调用幂等配置。
configure_http_client_logging()
