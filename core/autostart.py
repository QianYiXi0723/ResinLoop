# -*- coding: utf-8 -*-
"""开机自启（Windows 注册表 Run 键）。

为什么用 ``HKCU\\...\\CurrentVersion\\Run`` 而不是任务计划 / 启动文件夹：
    任务计划要管理员权限、还得输密码，改一次要重装；启动文件夹会闪黑框。
    HKCU 的 Run 键**不需要管理员权限**，登录时由 explorer.exe 静默执行，
    值就是一个命令行字符串，增删都只影响当前用户。

启动什么：
    ``pythonw.exe "<项目绝对路径>\\daemon.py"`` —— 守护进程自带右下角托盘、
    没有控制台窗口，开机后静默待在托盘里。用户要开界面时点托盘右键
    「打开控制台」，此时才会拉起 app.py 的窗口（app.py 会弹窗，不能拿来自启）。

关键细节：
    1. 必须用 ``pythonw.exe`` 的**绝对路径**：用 ``python.exe`` 会闪一个黑框；
       只写 ``pythonw.exe`` 依赖 PATH，开机时环境变量可能还没就绪。
    2. 脚本路径也必须绝对：注册表启动项的当前目录是 ``C:\\Windows\\System32``，
       相对路径必然找不到文件。
    3. 两个路径都用双引号包住，防止「Program Files」这类带空格的目录被截断。

路径迁移的处理：
    项目目录被移动 / 换了 Python 之后，注册表里的旧值会指向不存在的文件 ——
    开机时会静默失败，且用户完全看不出来。所以 :func:`status` 会比对
    "注册表里的命令" 与 "当前应该写的命令"，不一致时返回 ``stale=True``，
    界面上提示重新保存一次。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("dailycheck.autostart")

BASE_DIR = Path(__file__).resolve().parent.parent

#: 注册表路径（当前用户，无需管理员权限）
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: 值名。同名值只会有一条，重复写入等价于覆盖。
VALUE_NAME = "ResinLoop"
#: 自启的入口脚本（守护进程 + 托盘，无窗口）
TARGET_SCRIPT = "daemon.py"

#: 平台是否支持（非 Windows 只是不显示入口，不影响其它功能）
SUPPORTED = os.name == "nt"

_NO_WINDOW_HINT = (
    "仅支持 Windows（当前系统没有 HKCU Run 键）。macOS / Linux 请用系统的"
    "「登录项 / systemd --user」自行配置。"
)


# --------------------------------------------------------------------------- #
# 命令拼接
# --------------------------------------------------------------------------- #


def pythonw_path() -> Path:
    """pythonw.exe 的绝对路径；找不到时退回当前解释器。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return exe


def target_path() -> Path:
    """要自启的脚本绝对路径。"""
    return BASE_DIR / TARGET_SCRIPT


def build_command() -> str:
    """拼出写进注册表的完整命令行（两个路径都带引号）。"""
    return f'"{pythonw_path()}" "{target_path()}"'


# --------------------------------------------------------------------------- #
# 注册表读写
# --------------------------------------------------------------------------- #


def read_value() -> Optional[str]:
    """读当前注册表值；不存在 / 平台不支持返回 None。"""
    if not SUPPORTED:
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    except OSError as exc:  # 权限 / 注册表被策略锁住
        logger.warning("读取开机自启注册表失败：%s", exc)
        return None
    return str(value) if value is not None else None


def is_enabled() -> bool:
    """当前用户是否已经设置了本程序的开机自启。"""
    return read_value() is not None


def enable() -> Dict[str, Any]:
    """写入自启项（幂等：已存在就覆盖成当前命令）。"""
    if not SUPPORTED:
        return {"ok": False, "message": _NO_WINDOW_HINT}

    import winreg

    command = build_command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        logger.exception("写入开机自启失败")
        return {"ok": False, "message": f"写入注册表失败：{exc}"}

    logger.info("已开启开机自启：%s", command)
    return {
        "ok": True,
        "message": "已开启：下次登录 Windows 会自动在托盘启动",
        "value": command,
    }


def disable() -> Dict[str, Any]:
    """删除自启项（幂等：本来就没有也算成功）。"""
    if not SUPPORTED:
        return {"ok": False, "message": _NO_WINDOW_HINT}

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        logger.info("开机自启本来就是关闭状态")
        return {"ok": True, "message": "已关闭（本来就是关的）", "value": None}
    except OSError as exc:
        logger.exception("删除开机自启失败")
        return {"ok": False, "message": f"删除注册表项失败：{exc}"}

    logger.info("已关闭开机自启")
    return {"ok": True, "message": "已关闭：下次登录不会自动启动", "value": None}


# --------------------------------------------------------------------------- #
# 汇总状态（给 /api/autostart 用）
# --------------------------------------------------------------------------- #


def status() -> Dict[str, Any]:
    """当前状态 + 是否与当前路径一致（stale 表示注册表里是旧路径）。"""
    if not SUPPORTED:
        return {
            "ok": True,
            "supported": False,
            "enabled": False,
            "stale": False,
            "value": None,
            "command": "",
            "target": str(target_path()),
            "message": _NO_WINDOW_HINT,
        }

    current_value = read_value()
    expected = build_command()
    stale = bool(current_value) and current_value.strip().lower() != expected.strip().lower()

    return {
        "ok": True,
        "supported": True,
        "enabled": current_value is not None,
        # 注册表里是旧路径（搬过目录 / 换过 Python）：开机时会静默失败
        "stale": stale,
        "value": current_value,
        "command": expected,
        "target": str(target_path()),
        "message": (
            "注册表里是旧路径，开机可能起不来，请重新打开一次开关"
            if stale
            else ("已开启" if current_value else "未开启")
        ),
    }


def apply(enable_flag: Any) -> Dict[str, Any]:
    """按请求开关自启，返回最新状态。"""
    # 只有明确的真值才当"开"，避免字符串 "false" 被当成 True
    want = enable_flag is True or str(enable_flag).strip().lower() in ("1", "true", "on", "yes")

    result = enable() if want else disable()
    payload = status()
    payload.update({k: v for k, v in result.items() if k in ("ok", "message")})
    # ok 以注册表操作结果为准；状态里的 ok 只代表"读取成功"
    payload["ok"] = bool(result.get("ok"))
    return payload
