"""集中定义后端进程配置、跨字段安全约束与 Secret 文件读取。"""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_employee.domain.identity import MAX_SESSION_TTL_SECONDS, MIN_SESSION_TTL_SECONDS


class Settings(BaseSettings):
    """从环境变量与本地开发文件加载并验证后端进程配置。

    配置对象统一承载 API、任务执行、外部集成与模型网关所需参数。生产环境
    禁止启用测试适配器，避免合成数据或假实现进入真实业务流程；单步超时不得
    超过任务总超时，以保证 Worker 能在总截止时间前中止失控步骤。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_test_mode: bool = False
    app_base_url: str = "http://localhost:5173"
    database_url: str = "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee"
    checkpoint_database_url: str = "postgresql://ai_employee:ai_employee@localhost:5432/ai_employee"
    redis_url: str = "redis://localhost:6379/0"
    task_timeout_seconds: int = Field(default=900, ge=1)
    task_step_timeout_seconds: int = Field(default=300, ge=1)
    session_cookie_name: str = "ai_employee_session"
    session_ttl_seconds: int = Field(
        default=604800,
        ge=MIN_SESSION_TTL_SECONDS,
        le=MAX_SESSION_TTL_SECONDS,
    )
    app_master_key_file: Path = Path("/run/secrets/app_master_key")
    google_client_id: str = ""
    google_client_secret_file: Path = Path("/run/secrets/google_client_secret")
    google_redirect_uri: str = ""
    model_base_url: str = ""
    model_api_key_file: Path = Path("/run/secrets/model_api_key")
    model_name: str = ""
    model_supports_json_schema: bool = True
    model_input_cost_per_million_usd: float = Field(default=0, ge=0)
    model_output_cost_per_million_usd: float = Field(default=0, ge=0)
    model_redaction_patterns: list[str] = Field(default_factory=list)
    work_email_domains: list[str] = Field(default_factory=list)
    default_timezone: str = "Asia/Shanghai"
    default_brief_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("default_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """验证默认时区是运行环境可识别的 IANA 时区。

        Args:
            value: 待验证的 IANA 时区名称。

        Returns:
            经验证且保持原样的时区名称。

        Raises:
            ZoneInfoNotFoundError: 时区数据库中不存在指定名称。
        """
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> "Settings":
        """验证涉及多个配置项的运行模式与超时不变量。

        Returns:
            通过全部跨字段校验的当前配置对象。

        Raises:
            ValueError: 生产环境启用测试模式，或单步超时超过任务总超时时抛出。
        """
        # 测试适配器只提供合成事实，生产环境启用会破坏数据真实性边界。
        if self.app_env == "production" and self.app_test_mode:
            raise ValueError("APP_TEST_MODE cannot be enabled in production")
        # 单步截止时间必须受任务总截止时间约束，避免任务已超时但步骤仍在运行。
        if self.task_step_timeout_seconds > self.task_timeout_seconds:
            raise ValueError("TASK_STEP_TIMEOUT_SECONDS cannot exceed TASK_TIMEOUT_SECONDS")
        return self

    def read_secret_file(self, path: Path) -> SecretStr:
        """从指定 UTF-8 文件读取 Secret，并以遮蔽类型返回。

        Args:
            path: 由部署环境挂载的 Secret 文件路径。

        Returns:
            去除文件边界空白后的 ``SecretStr``，避免普通字符串意外出现在日志中。

        Raises:
            OSError: 文件不存在、权限不足或读取失败。
            UnicodeDecodeError: 文件内容不是有效 UTF-8 文本。
        """
        return SecretStr(path.read_text(encoding="utf-8").strip())


@lru_cache
def get_settings() -> Settings:
    """构建并缓存当前进程唯一的已验证配置对象。

    Returns:
        从环境变量及可选 ``.env`` 文件加载的配置实例。

    Raises:
        ValueError: 配置字段或跨字段安全约束验证失败。
    """
    return Settings()
