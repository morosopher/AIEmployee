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
from ai_employee.cli.database_maintenance import CalendarAadOwnerFactsReader
from ai_employee.config import Settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadCurrentGuard,
    CalendarAadFactsReader,
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
    """从固定 state 路径派生绑定；显式旧字段不得选择另一个 artifact。

    migration/resync 的批准 invocation 只提供 state 与实际 image ID。这里仅解析
    `/backups/<safe-basename>.calendar-aad-preflight.json` 并冻结非根物理目录；不检查
    artifact 存在性、碰撞或内容，这些事实仍必须在取得 revision lease 后读取。
    """
    state = os.environ.get("CALENDAR_AAD_ROLLOUT_STATE", "")
    suffix = ".calendar-aad-preflight.json"
    basename = Path(state).name.removesuffix(suffix)
    binding = CalendarAadBinding(basename, os.environ.get("CALENDAR_AAD_IMMUTABLE_IMAGE_ID", ""))
    if state != f"/backups/{binding.basename}{suffix}":
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
    directory = Path("/backups")
    explicit_directory = os.environ.get("BACKUP_DIR")
    explicit_basename = os.environ.get("BACKUP_ARTIFACT_BASENAME")
    if (explicit_directory is not None and Path(explicit_directory) != directory) or (
        explicit_basename is not None and explicit_basename != binding.basename
    ):
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
    try:
        directory = directory.resolve()
    except (OSError, RuntimeError) as error:
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid") from error
    if directory == Path("/"):
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
    return directory, binding


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
    facts_reader: CalendarAadFactsReader | None = None,
) -> CalendarAadArtifact:
    """取得固定 lease 后才读取 artifact，组合应用用例并在当前 guard 下原子发布。

    调用方拥有 sessions；本入口不创建通用 Worker、Taskiq、Redis consumer 或目录任务。
    整个调用（含最终 lease exit）和单 provider 步骤各有独立预算。发布的内部失败
    仍在 lease 内补偿；仅最终 exit 首次失败时，收完 exit 后按本次文件身份条件撤回。
    """
    binding = CalendarAadBinding(basename, immutable_image_id)
    # 构造只形成固定路径；首次文件检查仍严格位于取得同一 revision lease 之后。
    artifact_file = CalendarAadArtifactFile(backup_directory, binding)
    published = False
    try:
        async with (
            asyncio.timeout(task_timeout_seconds),
            async_calendar_aad_rollout_lease(sessions.engine.url) as lease,
        ):
            # artifact 同步读取也属于当前 lease；超时必须先收完线程，不能提前退出并复用连接。
            existing = await await_calendar_aad_resource(
                asyncio.create_task(asyncio.to_thread(artifact_file.read_optional))
            )
            repository = SqlAlchemyCalendarAadPreflightRepository(
                sessions, facts_reader=facts_reader
            )
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
            published = await artifact_file.publish(artifact, guard)
    except BaseException as failure:
        # 只有 publish 与内部清理已正常交付的新文件才来到此处。timeout 先完成原有转换，
        # 随后的补偿取消不能替换这个首次失败；退出线程也已由 lease adapter 收至终态。
        if published:
            rollback = asyncio.create_task(asyncio.to_thread(artifact_file.discard_new_publication))
            try:
                await await_calendar_aad_resource(rollback)
            except BaseException as rollback_failure:
                raise failure from rollback_failure
        raise
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
            facts_reader=CalendarAadOwnerFactsReader.from_environment(),
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
