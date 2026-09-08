"""在一个 revision session lease 内组合既有备份 producer 与最终 rollout guard。

仅为 sealed basename 入口提供 guard/临时产物交接。普通备份的加密、retention 和 remote
仍由既有 shell 函数拥有；本模块不定义 manifest、恢复协议或完整备份组生命周期。
"""

import asyncio
import hashlib
import os
import signal
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadBinding,
    CalendarAadRolloutError,
)
from ai_employee.cli.calendar_aad_preflight_0019 import (
    require_no_rollout_arguments,
    require_sealed_settings,
    rollout_environment,
)
from ai_employee.config import Settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadCurrentGuard,
    SqlAlchemyCalendarAadPreflightRepository,
    async_calendar_aad_rollout_lease,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "backup-postgres.sh"
# TERM 同时发送给独立组；父 shell 等待已接收 TERM 的子 shell，而 producer 再等待 pg_dump，
# 避免只等 leader 退出就误把尚在收尾的孙进程当作完成。
_PROGRAMS = {
    "produce": 'trap \'wait; exit 143\' TERM; source "$1"; create_encrypted_backup "$2"',
    "finish": 'trap \'wait; exit 143\' TERM; source "$1"; finish_existing_backup "$2"',
}


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """取消时终止自己的独立进程组，等待 shell/pg_dump/openssl 收尾后才删除暂存目录。"""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def run_backup_shell(operation: Literal["produce", "finish"], artifact: Path) -> None:
    """使用固定内部 source 入口复用 shell，argv 中只有受控路径且不输出子进程原始诊断。

    Args:
        operation: 唯一生产 dump/encrypt 或既有后处理分支；无任意命令入口。
        artifact: producer 的私有暂存路径，或已通过最终 guard 的公开 basename 产物。

    Raises:
        CalendarAadRolloutError: 子进程失败；保留稳定码而非可能含数据库信息的 stderr。
    """
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "bash",
            "-ec",
            _PROGRAMS[operation],
            "calendar-aad-backup",
            str(_SCRIPT),
            str(artifact),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    )
    try:
        process = await await_calendar_aad_resource(creation)
        if await process.wait() != 0:
            raise CalendarAadRolloutError("calendar_aad_backup_failed")
    except asyncio.CancelledError as cancellation:
        # spawn 结果在 helper 返回/抛出前已确定；即使赋值被取消也必须接回并停止自身进程组。
        if not creation.cancelled() and creation.exception() is None:
            process = creation.result()
            cleanup = asyncio.create_task(_stop_process(process))
            try:
                await await_calendar_aad_resource(cleanup)
            except asyncio.CancelledError:
                raise cancellation
        raise


def _check_collision(artifact: Path) -> None:
    """只在 revision lease/入口 guard 后检查；任何已有文件或悬空链接都不得覆盖。"""
    if os.path.lexists(artifact) or os.path.lexists(artifact.with_suffix(".enc.sha256")):
        raise CalendarAadRolloutError("calendar_aad_backup_collision")


def _prepare_checksum(artifact: Path) -> None:
    """非阻塞打开并校验 producer 的同一 regular fd，固定0600并生成无绝对路径 checksum。"""
    descriptor = os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise CalendarAadRolloutError("calendar_aad_backup_failed")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(
        artifact.with_suffix(".enc.sha256"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"{digest}  {artifact.name}\n")
        stream.flush()
        os.fsync(stream.fileno())


def _publish_backup(staged: Path, target: Path) -> None:
    """最终 guard 之后以 no-clobber 链接发布两个既有产物；失败只撤回本次创建的文件。

    两文件不是完整 manifest 组提交协议；完整组 publication 由后续备份任务定义。
    当前 hook 保证原 guard、内容 ID、basename 和 deadline 不会在 producer 后被跳过。
    """
    published: list[Path] = []
    try:
        for source, destination in (
            (staged, target),
            (staged.with_suffix(".enc.sha256"), target.with_suffix(".enc.sha256")),
        ):
            os.link(source, destination, follow_symlinks=False)
            published.append(destination)
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise


def _remove_published(target: Path) -> None:
    """只在本次 publish 已完成却收到取消时撤回自身新文件，碰撞分支不会调用。"""
    target.unlink(missing_ok=True)
    target.with_suffix(".enc.sha256").unlink(missing_ok=True)


async def run_guarded_backup(
    *,
    sessions: ManagedAsyncSessionMaker,
    backup_directory: Path,
    basename: str,
    immutable_image_id: str,
    clock: Callable[[], datetime],
    producer: Callable[[Path], Awaitable[None]],
) -> Path:
    """持同一 lease 验证原 artifact、调用 producer，再重读当前期限后发布精确 basename。

    producer 只能接收一个私有输出路径；调用方拥有数据库工厂和可取消进程。
    每个本地线程均须收回结果后才能退出 lease。任一 guard/producer/cleanup 失败或取消
    都清除自身暂存文件并撤回本次新发布；cleanup 的后续取消不得跳过下一项补偿。
    既有成功备份或 no-clobber 碰撞文件从不覆盖或删除。
    """
    binding = CalendarAadBinding(basename, immutable_image_id)
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        artifact_file = CalendarAadArtifactFile(backup_directory, binding)
        artifact = await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(artifact_file.read))
        )
        guard = CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=artifact_file,
            artifact=artifact,
            lease=lease,
            clock=clock,
            expected_revision="20260809_0018",
        )
        await guard.verify()
        target = backup_directory / f"{basename}.dump.enc"
        await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(_check_collision, target))
        )
        creation = asyncio.create_task(
            asyncio.to_thread(
                tempfile.TemporaryDirectory, prefix=".calendar-aad-backup-", dir=backup_directory
            )
        )
        publication: asyncio.Task[None] | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            try:
                directory = await await_calendar_aad_resource(creation)
                staged = Path(directory.name) / target.name
                await producer(staged)
                preparation = asyncio.create_task(asyncio.to_thread(_prepare_checksum, staged))
                await await_calendar_aad_resource(preparation)
                await guard.verify()
                publication = asyncio.create_task(
                    asyncio.to_thread(_publish_backup, staged, target)
                )
                await await_calendar_aad_resource(publication)
            except asyncio.CancelledError as error:
                cancellation = error
                raise
            finally:
                # 创建结果可能在取消后才返回，只有从已完成的 Task 接回目录才能完整清理。
                if not creation.cancelled() and creation.exception() is None:
                    directory = creation.result()
                    cleanup = asyncio.create_task(asyncio.to_thread(directory.cleanup))
                    try:
                        await await_calendar_aad_resource(cleanup)
                    except asyncio.CancelledError:
                        if cancellation is not None:
                            raise cancellation
                        raise
        except BaseException as failure:
            # 目录清理自身收到取消也属于失败发布；只有本次成功发布的两个成员需要撤回。
            # _publish_backup 自身失败时已经撤回部分新成员，不能删除导致碰撞的既有文件。
            if (
                publication is not None
                and not publication.cancelled()
                and publication.exception() is None
            ):
                rollback = asyncio.create_task(asyncio.to_thread(_remove_published, target))
                try:
                    await await_calendar_aad_resource(rollback)
                except asyncio.CancelledError:
                    if isinstance(failure, asyncio.CancelledError):
                        raise failure
                    raise
            raise
        return target


async def _run(settings: Settings, directory: Path, binding: CalendarAadBinding) -> None:
    """复用 app Secret 身份读取0018，guard通过后才进入既有 retention/remote 后处理。"""
    sessions = build_session_factory(settings.database_url)
    try:
        artifact = await run_guarded_backup(
            sessions=sessions,
            backup_directory=directory,
            basename=binding.basename,
            immutable_image_id=binding.immutable_image_id,
            clock=lambda: datetime.now(UTC),
            producer=lambda path: run_backup_shell("produce", path),
        )
        await run_backup_shell("finish", artifact)
    finally:
        await sessions.dispose()


def main(arguments: Sequence[str] | None = None) -> int:
    """无参数备份 hook；宿主解析的 image ID 是唯一镜像绑定，错误只含稳定码。"""
    try:
        require_no_rollout_arguments(arguments)
        directory, binding = rollout_environment()
        settings = Settings()
        require_sealed_settings(settings)
        asyncio.run(_run(settings, directory, binding))
    except (DomainError, SQLAlchemyError, OSError, ValueError, RuntimeError) as error:
        print(
            error.error_code if isinstance(error, DomainError) else "calendar_aad_backup_failed",
            file=sys.stderr,
        )
        return 1
    print("calendar_aad_backup_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
