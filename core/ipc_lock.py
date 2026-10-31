# -*- coding: utf-8 -*-
"""跨进程文件锁（Windows 专用实现；本项目只发行 Windows）。

语义与 filelock 库同族，由内核保证：
- 锁加在「打开中的文件描述符」的首字节上，持有到 close；
- **进程无论优雅退出还是被杀，内核自动释放——不存在陈旧锁**。
  这是它与 pid/心跳文件方案的本质区别（后者有 TOCTOU 与陈旧误判两宗罪）。

两种用法：
- daemon 单实例：``try_acquire``（非阻塞抢占，抢不到立即认输），
  返回的 fd 由调用方持有到进程结束；
- 读改写临界区：``blocking``（msvcrt.LK_LOCK 内建每秒重试 ×10，超时抛 OSError）。

注意：不要手搓 fcntl 分支或 O_EXCL+pid 方案——前者跨平台坑多、后者
崩溃后留陈旧文件（检索结论，2026-10-31）。
"""
import msvcrt
import os
from contextlib import contextmanager

__all__ = ["try_acquire", "blocking"]


def _open_lock_file(path: str) -> int:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return os.open(path, os.O_CREAT | os.O_RDWR)


def _lock_fd(fd: int, *, blocking: bool, unlock: bool = False) -> None:
    """对 fd 首字节加/解锁（区域可为零长度文件——Windows 允许锁 EOF 之后）。

    LK_LOCK：被占用时每秒重试、10 次后抛 OSError（约 10 秒阻塞上限）；
    LK_NBLCK：立即抛 OSError。"""
    mode = msvcrt.LK_UNLCK if unlock else (msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK)
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, mode, 1)


def try_acquire(path: str):
    """非阻塞抢占 path 的排它锁。

    成功 → 返回持有中的 fd（调用方负责在进程存活期不关闭它）；
    被其它进程持有 → 返回 None。"""
    fd = _open_lock_file(path)
    try:
        _lock_fd(fd, blocking=False)
        return fd
    except OSError:
        os.close(fd)
        return None


@contextmanager
def blocking(path: str):
    """阻塞互斥上下文（读改写临界区用）。"""
    fd = _open_lock_file(path)
    try:
        _lock_fd(fd, blocking=True)
        try:
            yield
        finally:
            try:
                _lock_fd(fd, blocking=True, unlock=True)
            except OSError:
                pass  # 解锁失败只影响下次获取速度，不影响正确性（close 必释）
    finally:
        os.close(fd)
