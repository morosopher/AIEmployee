"""为历史 0019 CLI 集成测试复制真实发布脚本，不替代生产发布权威校验。"""

from pathlib import Path
from shutil import copyfile

from alembic.config import Config
from alembic.script import ScriptDirectory

from ai_employee.infrastructure.db.alembic import load_published_alembic_authority


def freeze_calendar_aad_0019_release(tmp_path: Path) -> Path:
    """创建仅含已验证 0019 完整祖先链的临时 Alembic 配置。

    Args:
        tmp_path: pytest 拥有并清理的临时目录；不读取或修改数据库及受管准入配置。

    Returns:
        临时 ini 路径，供真实 CLI 再次加载发布链并执行原 env、revision 和三锁协议。

    原脚本逐字节复制；先验证当前发布链，再重新验证副本的完整线性链。仅历史测试
    显式使用该路径，当前镜像拒绝测试继续加载仓库配置，不能用手填 head 模拟权威。
    """
    backend_root = Path(__file__).resolve().parents[3]
    source_config = Config(backend_root / "alembic.ini")
    published = load_published_alembic_authority(source_config)
    assert published.contains("20260809_0019")
    historical_revisions = published.revisions[: published.revisions.index("20260809_0019") + 1]
    source_directory = ScriptDirectory.from_config(source_config)
    release_root = tmp_path / "calendar-aad-0019-release"
    versions = release_root / "migrations" / "versions"
    versions.mkdir(parents=True)
    for revision in historical_revisions[1:]:
        script = source_directory.get_revision(revision)
        assert script is not None
        source = Path(script.path)
        destination = versions / source.name
        copyfile(source, destination)
        assert destination.read_bytes() == source.read_bytes()
    copyfile(Path(source_directory.dir) / "env.py", release_root / "migrations" / "env.py")
    config_path = release_root / "alembic.ini"
    copyfile(backend_root / "alembic.ini", config_path)
    historical_config = Config(config_path)
    historical_config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    with config_path.open("w", encoding="utf-8") as stream:
        historical_config.file_config.write(stream)
    frozen = load_published_alembic_authority(Config(config_path))
    assert frozen.revisions == historical_revisions
    assert frozen.head_revision == "20260809_0019"
    return config_path
