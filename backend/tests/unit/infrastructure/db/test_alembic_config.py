"""验证 Alembic 数据库 URL 配置边界的百分号处理。"""

from alembic.config import Config

from ai_employee.infrastructure.db.alembic import set_alembic_database_url


def test_percent_encoded_database_url_round_trips_through_alembic_config() -> None:
    """验证百分号编码与连续百分号经 Alembic 插值层后保持原值。"""
    database_url = (
        "postgresql+asyncpg://user:password%40value@db.invalid/app_test?label=100%%ready"
    )
    config = Config()

    set_alembic_database_url(config, database_url)

    assert config.get_main_option("sqlalchemy.url") == database_url
