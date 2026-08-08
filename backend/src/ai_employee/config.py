"""集中定义后端进程配置、跨字段安全约束与 Secret 文件读取。"""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_employee.domain.connections import parse_provider_identity_key
from ai_employee.domain.identity import MAX_SESSION_TTL_SECONDS, MIN_SESSION_TTL_SECONDS


class Settings(BaseSettings):
    """从环境变量与本地开发文件加载并验证后端进程配置。

    配置对象统一承载 API、任务执行、外部集成与模型网关所需参数。生产环境
    禁止启用测试适配器，避免合成数据或假实现进入真实业务流程；单步超时不得
    超过任务总超时，以保证 Worker 能在总截止时间前中止失控步骤。M2 真实写入
    默认关闭，并由全局开关、供应商开关及适用环境的专用账户白名单共同收窄。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    app_test_mode: bool = False
    app_base_url: str = "http://localhost:5173"
    external_writes_enabled: bool = False
    google_writes_enabled: bool = False
    microsoft_writes_enabled: bool = False
    write_test_account_allowlist: list[str] = Field(default_factory=list)
    database_url: str = "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee"
    checkpoint_database_url: str = "postgresql://ai_employee:ai_employee@localhost:5432/ai_employee"
    redis_url: str = "redis://localhost:6379/0"
    task_timeout_seconds: int = Field(default=900, ge=1)
    task_step_timeout_seconds: int = Field(default=300, ge=1)
    task_lease_seconds: int = Field(
        default=360,
        ge=1,
        description="Worker 执行租约与节点间续租使用的正整数秒数。",
    )
    task_retry_recovery_seconds: int = Field(
        default=360,
        ge=1,
        description=(
            "Outbox relay 已确认把延迟重试交给 Redis 后，PostgreSQL 在 Redis 丢失时等待的"
            "恢复秒数；该期限不用于推断或限制 relay 的交接耗时。"
        ),
    )
    outbox_claim_seconds: int = Field(
        default=60,
        ge=1,
        description="Outbox 在外部投递前向后移动 available_at 的短 claim 秒数。",
    )
    outbox_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        description="Outbox 首次安全投递失败后的指数退避基准秒数。",
    )
    outbox_retry_max_seconds: int = Field(
        default=300,
        ge=1,
        description="Outbox 指数退避允许达到的最大秒数。",
    )
    outbox_relay_batch_size: int = Field(
        default=100,
        ge=1,
        description="单次 Outbox relay 事务最多 claim 的未发布事件数。",
    )
    otel_enabled: bool = False
    otel_service_name: str = "ai-employee"
    otel_exporter_otlp_endpoint: str = ""
    metrics_enabled: bool = True
    worker_metrics_port: int = Field(default=9101, ge=1, le=65535)
    scheduler_metrics_port: int = Field(default=9102, ge=1, le=65535)
    retention_database_url_file: Path = Path("/run/secrets/retention_database_url")
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
    microsoft_client_id: str = ""
    microsoft_client_secret_file: Path = Path("/run/secrets/microsoft_client_secret")
    microsoft_redirect_uri: str = ""
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

    @field_validator("write_test_account_allowlist")
    @classmethod
    def validate_write_test_account_allowlist(cls, value: list[str]) -> list[str]:
        """验证专用测试账户使用稳定且规范化的供应商连接身份键。

        身份键固定为 ``provider:provider_tenant_id:provider_account_id``。Google
        可使用空 tenant，形成 ``google::account``；邮箱等可变显示标识不得进入
        白名单。错误信息只指出配置项，不回显具体身份，避免敏感连接标识进入日志。

        Args:
            value: 从环境变量解析出的供应商连接身份键列表。

        Returns:
            保持原顺序的已验证身份键列表，供后续执行边界做精确匹配。

        Raises:
            ValueError: 任一条目不是支持供应商的规范化稳定身份键时抛出。
        """
        for provider_identity_key in value:
            try:
                parse_provider_identity_key(provider_identity_key)
            except (TypeError, ValueError):
                raise ValueError(
                    "WRITE_TEST_ACCOUNT_ALLOWLIST entries must use normalized provider identity keys"
                ) from None
        return value

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> "Settings":
        """验证涉及多个配置项的运行模式与超时不变量。

        Returns:
            通过全部跨字段校验的当前配置对象。

        Raises:
            ValueError: 运行模式、真实写入门禁或任务时序配置互相冲突时抛出。
        """
        # 测试适配器只提供合成事实，生产环境启用会破坏数据真实性边界。
        if self.app_env == "production" and self.app_test_mode:
            raise ValueError("APP_TEST_MODE cannot be enabled in production")
        # 单步截止时间必须受任务总截止时间约束，避免任务已超时但步骤仍在运行。
        if self.task_step_timeout_seconds > self.task_timeout_seconds:
            raise ValueError("TASK_STEP_TIMEOUT_SECONDS cannot exceed TASK_TIMEOUT_SECONDS")
        # 当前 Worker 只在节点边界续租；租约短于单步预算会允许第二 Worker 在节点中途接管。
        if self.task_lease_seconds < self.task_step_timeout_seconds:
            raise ValueError("TASK_LEASE_SECONDS cannot be shorter than TASK_STEP_TIMEOUT_SECONDS")
        # 初始退避高于上限会让配置语义自相矛盾，也会使首次失败无法遵守最大等待值。
        if self.outbox_retry_base_seconds > self.outbox_retry_max_seconds:
            raise ValueError("OUTBOX_RETRY_BASE_SECONDS cannot exceed OUTBOX_RETRY_MAX_SECONDS")
        # 非生产真实写入必须再绑定专用账户，防止测试或预发布误触个人/生产连接。
        if (
            self.app_env != "production"
            and self.external_writes_enabled
            and (self.google_writes_enabled or self.microsoft_writes_enabled)
            and not self.write_test_account_allowlist
        ):
            raise ValueError(
                "WRITE_TEST_ACCOUNT_ALLOWLIST is required when non-production writes are enabled"
            )
        return self

    def provider_writes_enabled(self, provider: str) -> bool:
        """判断固定供应商是否同时通过全局与供应商级真实写入开关。

        Args:
            provider: 内部规范化供应商名称；当前仅接受 ``google`` 或 ``microsoft``。

        Returns:
            仅当全局开关和对应供应商开关都开启时返回 ``True``；未知供应商始终
            返回 ``False``，避免新增字符串值意外继承写能力。
        """
        if not self.external_writes_enabled:
            return False
        if provider == "google":
            return self.google_writes_enabled
        if provider == "microsoft":
            return self.microsoft_writes_enabled
        return False

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """判断规范化供应商连接身份是否通过当前环境的账户限制。

        Args:
            provider_identity_key: 待核对的稳定连接身份键，格式由连接层规范化为
                ``provider:provider_tenant_id:provider_account_id``。

        Returns:
            输入不是规范化身份键时始终返回 ``False``；身份键通过 parser 后，白名单非空
            时仅返回精确成员匹配结果，白名单为空时仅生产环境返回 ``True``。调用方仍须
            独立验证全局、供应商、连接能力与精确人工审批。
        """
        # 即使生产环境省略额外 allowlist，也不能让格式异常的候选绕过身份边界；复用同一
        # parser 可保证候选与配置条目拥有完全相同的 provider/tenant/account 约束。
        try:
            parse_provider_identity_key(provider_identity_key)
        except (TypeError, ValueError):
            return False
        if self.write_test_account_allowlist:
            return provider_identity_key in self.write_test_account_allowlist
        return self.app_env == "production"

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
