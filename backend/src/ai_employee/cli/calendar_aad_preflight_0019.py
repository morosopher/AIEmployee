"""无 scope 参数的 0018 主动刷新 preflight 组合根，输出只含稳定计数与 digest。"""

import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from httpx import HTTPError
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.ports.oauth_refresh import OAuthRefreshCoordinator
from ai_employee.application.use_cases.calendar_aad_preflight import (
    CalendarAadAdapters,
    CalendarAadPreflightUseCase,
)
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadArtifact,
    CalendarAadBinding,
    CalendarAadRolloutError,
    serialize_rollout_artifact,
)
from ai_employee.config import Settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadCurrentGuard,
    SqlAlchemyCalendarAadPreflightRepository,
    async_calendar_aad_rollout_lease,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.integrations.registry import build_oauth_security_services
from ai_employee.workers.sync_calendar import CalendarAadReadAdapters


def require_no_rollout_arguments(arguments: Sequence[str] | None) -> None:
    """三个公开维护命令均无参数；拒绝时不回显可能含原始 scope/credential 的 argv。"""
    if arguments if arguments is not None else sys.argv[1:]:
        raise CalendarAadRolloutError("calendar_aad_arguments_invalid")


def rollout_environment() -> tuple[Path, CalendarAadBinding]:
    """只解析安全路径形状与内部 image ID；文件存在/碰撞检查必须在 revision lease 之后。"""
    directory = Path(os.environ.get("BACKUP_DIR", ""))
    if not directory.is_absolute() or directory == Path("/"):
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
    return directory, CalendarAadBinding(
        os.environ.get("BACKUP_ARTIFACT_BASENAME", ""),
        os.environ.get("CALENDAR_AAD_IMMUTABLE_IMAGE_ID", ""),
    )


def require_sealed_settings(settings: Settings) -> None:
    """维护窗口继续关闭全部真实写入开关；不允许 one-off 意外打开外部执行路径。"""
    if (
        settings.external_writes_enabled
        or settings.google_writes_enabled
        or settings.microsoft_writes_enabled
    ):
        raise CalendarAadRolloutError("calendar_aad_write_switch_enabled")


async def run_preflight(
    *,
    sessions: ManagedAsyncSessionMaker,
    coordinator: OAuthRefreshCoordinator,
    adapters: CalendarAadAdapters,
    backup_directory: Path,
    basename: str,
    immutable_image_id: str,
    clock: Callable[[], datetime],
    task_timeout_seconds: float = 900,
    step_timeout_seconds: float = 300,
) -> CalendarAadArtifact:
    """取得固定 lease 后才读取 artifact，组合应用用例并在当前 guard 下原子发布。

    调用方拥有 sessions；本入口不创建通用 Worker、Taskiq、Redis consumer 或目录任务。
    整个调用（含最终文件发布）和单 provider 步骤各有独立预算。
    """
    binding = CalendarAadBinding(basename, immutable_image_id)
    async with (
        asyncio.timeout(task_timeout_seconds),
        async_calendar_aad_rollout_lease(sessions.engine.url) as lease,
    ):
        artifact_file = CalendarAadArtifactFile(backup_directory, binding)
        existing = await asyncio.to_thread(artifact_file.read_optional)
        repository = SqlAlchemyCalendarAadPreflightRepository(sessions)
        artifact = await CalendarAadPreflightUseCase(
            store=repository,
            coordinator=coordinator,
            adapters=adapters,
            lease=lease,
            clock=clock,
            step_timeout_seconds=step_timeout_seconds,
            task_timeout_seconds=task_timeout_seconds,
        ).execute(binding=binding, existing=existing)
        guard = CalendarAadCurrentGuard(
            repository=repository,
            artifact_file=artifact_file,
            artifact=artifact,
            lease=lease,
            clock=clock,
            expected_revision="20260809_0018",
            allow_missing=True,
        )
        await artifact_file.publish(artifact, guard)
        return artifact


async def _run(
    settings: Settings, directory: Path, binding: CalendarAadBinding
) -> CalendarAadArtifact:
    """从与 API/Worker 相同的单 Secret 组合 AEAD、identity 和唯一 coordinator。"""
    sessions = build_session_factory(settings.database_url)
    try:
        security = await asyncio.to_thread(
            build_oauth_security_services,
            session_factory=sessions,
            master_key_file=settings.app_master_key_file,
        )
        clock = lambda: datetime.now(UTC)
        return await run_preflight(
            sessions=sessions,
            coordinator=security.coordinator,
            adapters=CalendarAadReadAdapters(settings, clock=clock),
            backup_directory=directory,
            basename=binding.basename,
            immutable_image_id=binding.immutable_image_id,
            clock=clock,
            task_timeout_seconds=settings.task_timeout_seconds,
            step_timeout_seconds=settings.task_step_timeout_seconds,
        )
    finally:
        await sessions.dispose()


def main(arguments: Sequence[str] | None = None) -> int:
    """固定无参数 CLI；数据库/供应商/Secret 异常只返回稳定脱敏错误码。"""
    try:
        require_no_rollout_arguments(arguments)
        directory, binding = rollout_environment()
        settings = Settings()
        require_sealed_settings(settings)
        artifact = asyncio.run(_run(settings, directory, binding))
    except (DomainError, SQLAlchemyError, HTTPError, OSError, ValueError, RuntimeError) as error:
        print(
            error.error_code if isinstance(error, DomainError) else "calendar_aad_rollout_failed",
            file=sys.stderr,
        )
        return 1
    print(serialize_rollout_artifact(artifact).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
