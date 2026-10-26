#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面应用入口 —— 把本地网页控制台装进一个独立窗口（pywebview）。

为什么要这一层：
    浏览器标签页容易被关掉、被别的标签淹没，也不像"软件"。
    这里用 pywebview（Windows 上走系统自带的 WebView2）开一个原生窗口，
    里面加载的还是同一份 http://127.0.0.1:8765 页面 —— 前端一行不用改。

结构：
    主线程  → pywebview 的窗口消息循环（GUI 必须在主线程）
    子线程  → http.server 控制台服务（login_ui.serve）
    独立进程 → daemon.py 的守护进程与托盘图标（start.bat 负责拉起）

用法：
    python app.py                 # 启动控制台并弹出窗口
    python app.py --port 8800     # 换端口
    python app.py --browser       # 不用窗口，退回默认浏览器
    python app.py --debug         # 打开 WebView 的开发者工具

关掉窗口 = 退出程序（只关控制台，守护进程仍在后台跑定时任务）。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "desktop.log"

WINDOW_TITLE = "游戏日常提醒控制台"
WINDOW_WIDTH = 980
WINDOW_HEIGHT = 800
WINDOW_MIN = (820, 620)

logger = logging.getLogger("dailycheck.app")


# --------------------------------------------------------------------------- #
# 运行环境
# --------------------------------------------------------------------------- #


class _NullStream:
    """pythonw.exe 下 sys.stdout / sys.stderr 是 None，写入会炸。

    统一塞一个黑洞流，任何 print / 第三方库的 debug 输出都不会打断界面。
    """

    def write(self, _data: Any) -> int:  # noqa: D401
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no fileno under pythonw")

    @property
    def encoding(self) -> str:
        return "utf-8"


def _silence_std_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]


def setup_logging(debug: bool = False) -> None:
    """桌面版没有控制台，日志一律落盘到 logs/desktop.log。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)


def _message_box(text: str, title: str = WINDOW_TITLE, icon: int = 0x40) -> None:
    """出错时给一个原生弹窗 —— pythonw 下没有控制台，否则用户什么都看不到。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:  # noqa: BLE001
        logger.debug("弹窗失败（已忽略）", exc_info=True)


# --------------------------------------------------------------------------- #
# 控制台服务
# --------------------------------------------------------------------------- #


def ensure_console(port: int, *, reuse: bool = True) -> Dict[str, Any]:
    """启动网页控制台（后台线程）；已有实例在跑就直接复用。

    返回结构与 :func:`login_ui.serve` 一致，另加两个键：

    ``owned``  —— 这个服务是不是本进程起的（决定退出时要不要关）
    ``reused`` —— 是否复用了别人已经在跑的实例
    """
    import login_ui

    if reuse and login_ui.console_alive(port):
        url = f"http://127.0.0.1:{port}/"
        logger.info("已有控制台在 %s 运行，直接复用", url)
        return {
            "server": None,
            "port": port,
            "url": url,
            "thread": None,
            "tools": None,
            "owned": False,
            "reused": True,
        }

    info = login_ui.serve(port)
    info["owned"] = True
    info["reused"] = False
    logger.info("控制台已启动：%s（进程内线程）", info["url"])
    return info


def wait_until_ready(port: int, timeout: float = 10.0) -> bool:
    """等 HTTP 服务真的能响应 —— 免得窗口先打开、页面转圈或报连接失败。"""
    import login_ui

    deadline = time.time() + timeout
    while time.time() < deadline:
        if login_ui.console_alive(port, timeout=0.6):
            return True
        time.sleep(0.15)
    return False


# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #


def run_window(url: str, *, debug: bool = False) -> bool:
    """弹出独立窗口并阻塞到用户关窗；失败返回 False 由调用方降级。"""
    try:
        import webview  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("pywebview 不可用（%s），将退回浏览器模式", exc)
        return False

    def _reveal(window: Any) -> None:
        """窗口起来后显式 show 一次。

        为什么要这一步：如果进程是用带 ``CREATE_NO_WINDOW`` 的启动方式
        （某些快捷方式、计划任务、父进程的 subprocess 调用）拉起来的，
        Windows 会把 ``SW_HIDE`` 写进 STARTUPINFO，GUI 的首个窗口就继承
        "不显示" —— 现象是进程活着、端口在听，但屏幕上看不到窗口。
        这里主动 show，把这种坑兜住。
        """
        try:
            time.sleep(0.4)
            window.show()
            restore = getattr(window, "restore", None)
            if callable(restore):
                restore()
            logger.info("窗口已显示")
        except Exception:  # noqa: BLE001
            logger.debug("显示窗口时出错（已忽略）", exc_info=True)

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=WINDOW_MIN,
            resizable=True,
            text_select=True,
            confirm_close=False,
            background_color="#F5F6F8",
        )
        logger.info("窗口已创建（%dx%d）", WINDOW_WIDTH, WINDOW_HEIGHT)
        webview.start(
            _reveal,
            window,
            debug=debug,
            private_mode=False,
            storage_path=str(LOGS_DIR / "webview"),
        )
        logger.info("窗口已关闭")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("窗口启动失败")
        return False


def _keep_alive(console: Dict[str, Any]) -> None:
    """降级到浏览器时，服务得一直活着，否则页面刷新就 404。"""
    thread = console.get("thread")
    try:
        while thread is None or thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: Optional[list] = None) -> int:
    _silence_std_streams()

    parser = argparse.ArgumentParser(description="游戏日常提醒 · 桌面控制台")
    parser.add_argument("--port", type=int, default=8765, help="控制台端口，默认 8765")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="不开独立窗口，改用默认浏览器（调试 / 兼容用）",
    )
    parser.add_argument("--debug", action="store_true", help="开开发者工具并写 DEBUG 日志")
    parser.add_argument(
        "--new-instance",
        action="store_true",
        help="即使已有控制台在跑，也另起一个（端口自动往后挪）",
    )
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    logger.info("=" * 60)
    logger.info("桌面控制台启动（pid=%s，python=%s）", os.getpid(), sys.executable)

    try:
        console = ensure_console(args.port, reuse=not args.new_instance)
    except Exception as exc:  # noqa: BLE001
        logger.exception("控制台启动失败")
        _message_box(f"控制台启动失败：\n\n{exc}\n\n详细日志：\n{LOG_FILE}", icon=0x10)
        return 1

    url = console["url"]
    if not wait_until_ready(console["port"]):
        logger.error("控制台端口 %s 在 10 秒内没有就绪", console["port"])

    ok = False
    if not args.browser:
        ok = run_window(url, debug=args.debug)

    if not ok:
        logger.warning("进入浏览器降级模式：%s", url)
        _message_box(
            "独立窗口没能启动，已改用默认浏览器打开控制台。\n\n"
            "常见原因：缺少 Microsoft Edge WebView2 运行时。\n"
            "可到微软官网下载安装后重试；不影响当前使用。",
            icon=0x30,
        )
        webbrowser.open(url)
        _keep_alive(console)

    if console.get("owned"):
        try:
            import login_ui

            login_ui.stop_server(console)
            logger.info("控制台服务已关闭")
        except Exception:  # noqa: BLE001
            logger.debug("关闭控制台时出错（已忽略）", exc_info=True)

    logger.info("桌面控制台退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
