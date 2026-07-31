"""封装应用数据库 URL 写入 Alembic 配置时的插值边界。"""

from alembic.config import Config


def set_alembic_database_url(config: Config, database_url: str) -> None:
    """将数据库 URL 无损写入 Alembic 主配置。

    Alembic 的 ``Config.set_main_option`` 使用 ``ConfigParser`` 的百分号插值规则，
    因此 URL 编码产生的字面 ``%`` 必须在写入时加倍。随后通过 Alembic 读取配置时，
    插值层会把 ``%%`` 还原成单个百分号，使 SQLAlchemy 最终收到调用方提供的原始 URL。
    本函数不解析、连接或记录 URL，避免在迁移配置边界暴露数据库凭据。

    Args:
        config: 接收数据库 URL 的 Alembic 配置对象。
        database_url: 已由调用方提供并完成 URL 编码的 SQLAlchemy 数据库 URL。
    """

    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
