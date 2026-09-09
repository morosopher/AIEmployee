"""只为 Task27C 的 artifact/dump/checksum 补偿保管目录项，避免删除公开路径的替换者。

公开 basename 可以被并发替换；随机 0700 保管目录及其成员则由本次调用独占。目录 fd
固定捕获后的命名空间，不防御主动侵入私有树的同 UID/特权进程，也不冻结已打开 fd 的
原地写入。未知捕获结果或无法无覆盖返还的外来对象保留原 basename，交由运维人工处置。
本模块只执行调用方线程中的有界本地补偿，不创建 Task、lease、重试或保留清理任务。
"""

import ctypes
import errno
import os
import stat
import tempfile
from pathlib import Path

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError


def _same_publication(current: os.stat_result, identity: os.stat_result) -> bool:
    """只匹配本次 regular 文件的 inode 与发布版本；已可见的原地改写不再归本次补偿。"""
    return (
        stat.S_ISREG(identity.st_mode)
        and os.path.samestat(current, identity)
        and current.st_mode == identity.st_mode
        and current.st_mtime_ns == identity.st_mtime_ns
        and current.st_size == identity.st_size
    )


def _return_without_replacement(name: str, custody_fd: int, parent_fd: int) -> None:
    """以 Linux 固定 RENAME_NOREPLACE 原子返还外来目录项，绝不先检查再覆盖或 unlink。

    ctypes 只绑定当前进程 libc 的一个固定文件操作，不加载外部库路径。系统或文件系统
    不支持时抛出 OSError，调用方须保留保管目录，不能退回非原子的检查/覆盖组合。
    """
    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename = library.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "calendar aad atomic return unavailable") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    encoded = os.fsencode(name)
    # RENAME_NOREPLACE=1：原位置任何已有目录项（包括悬空链接）都使返还失败。
    if rename(custody_fd, encoded, parent_fd, encoded, 1) != 0:
        raise OSError(ctypes.get_errno(), "calendar aad atomic return failed")


def _discard_captured(name: str, identity: os.stat_result, parent_fd: int, custody_fd: int) -> None:
    """原子捕获后才在私有命名空间验证并删除，捕获失败只能清掉仍被证明的自有占位。

    regular 占位让并发替换成目录的来源在 rename 时被拒绝。即使 rename 已生效却报告
    失败，也不能用尚未更新的 Python 标志把捕获物认作占位并删除；需在同一私有 fd
    重新证明占位身份，否则保守留下原 basename。外来内容只用 no-follow metadata。
    """
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=custody_fd
    )
    try:
        placeholder = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rename(name, name, src_dir_fd=parent_fd, dst_dir_fd=custody_fd)
    except OSError as failure:
        current = os.stat(name, dir_fd=custody_fd, follow_symlinks=False)
        if _same_publication(current, placeholder):
            os.unlink(name, dir_fd=custody_fd)
            if isinstance(failure, FileNotFoundError):
                return
        raise
    # 先固化保管位置再处理捕获物；中途失败只保留证据，不以猜测的捕获状态继续删除。
    os.fsync(custody_fd)
    os.fsync(parent_fd)
    current = os.stat(name, dir_fd=custody_fd, follow_symlinks=False)
    if _same_publication(current, identity):
        os.unlink(name, dir_fd=custody_fd)
    else:
        _return_without_replacement(name, custody_fd, parent_fd)
    os.fsync(custody_fd)
    os.fsync(parent_fd)


def discard_calendar_aad_publication(path: Path, identity: os.stat_result) -> None:
    """只撤回该次发布的文件，公开路径被替换后也不会删除替换者。

    Args:
        path: 当前 Task27C artifact、dump 或 checksum 的固定公开路径。
        identity: 调用方在自己发布时取得并保留的真实 regular 文件身份。

    Raises:
        CalendarAadRolloutError: 捕获、身份校验、无覆盖返还或清理失败。外来/未知内容
            保留在同目录 `.calendar-aad-custody-*/<原 basename>`，不自动回收或重试。

    预检查只跳过已知外来/消失文件，不能授权公开路径删除。实际删除只能使用打开的
    私有目录 fd；正常退出移除空保管目录，异常时也只尝试 rmdir，不递归清理未知内容。
    调用方继续拥有其线程 Task，并须在后来取消时等待本同步作用域收敛。
    """
    try:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if not _same_publication(current, identity):
            return
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            custody = Path(tempfile.mkdtemp(prefix=".calendar-aad-custody-", dir=path.parent))
            try:
                custody_fd = os.open(custody, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    _discard_captured(path.name, identity, parent_fd, custody_fd)
                finally:
                    os.close(custody_fd)
            except BaseException as failure:
                # 任意失败可能已捕获未知文件。只允许删除空目录；保留原失败及清理原因。
                try:
                    custody.rmdir()
                except OSError as cleanup_failure:
                    raise failure from cleanup_failure
                raise
            else:
                custody.rmdir()
                os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as failure:
        raise CalendarAadRolloutError("calendar_aad_publication_compensation_failed") from failure
