#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ResinLoop 引擎入口（exe 发行版专用）——纯本地 HTTP 服务，无任何窗口。

由 C# 壳（shell/ResinLoop.exe）以 ``pythonw.exe engine/app.py`` 静默拉起：
    1. 若目标端口已有控制台在跑，直接待命退出（壳会复用现有实例）；
    2. 启动 login_ui.serve（控制台页面 + 全部 /api + 可选的 APK 局域网下载服务）；
    3. 原地阻塞，直到被壳结束进程树（或 Ctrl+C）。

窗口、托盘、开机自启全部由 C# 壳负责；脚本模式的旧 app.py（pywebview 窗口）
保留在源码仓库里，与本文件并存互不影响。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import login_ui

logger = logging.getLogger("dailycheck.engine")

LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "engine.log"


def setup_logging() -> None:
    """引擎日志独立落盘（engine/logs/engine.log），壳崩溃时也有据可查。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=500_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ResinLoop · 本地引擎（无界面）")
    parser.add_argument("--port", type=int, default=8765, help="控制台端口，默认 8765")
    parser.add_argument(
        "--no-lan-tools", action="store_true",
        help="不启动 APK 局域网下载服务",
    )
    args = parser.parse_args(argv)

    setup_logging()
    logger.info("=" * 56)
    logger.info("引擎启动（pid=%s，python=%s）", os.getpid(), sys.executable)

    # 端口已有实例在跑：壳会复用它，引擎不再重复起服务（避免 pick_port 漂到别的端口）
    if login_ui.console_alive(args.port):
        logger.info("端口 %d 已有控制台实例，引擎待命退出（复用现有实例）", args.port)
        return 0

    try:
        info = login_ui.serve(args.port, lan_tools=not args.no_lan_tools)
    except Exception as exc:  # noqa: BLE001
        logger.exception("引擎服务启动失败：%s", exc)
        return 1

    logger.info("引擎已就绪：%s（等待壳加载页面）", info["url"])
    waiter = threading.Event()
    try:
        while True:
            # 原地常驻；停机由 C# 壳结束进程树（shutdown 挂在 finally 兜底）
            waiter.wait(3600)
    except KeyboardInterrupt:
        logger.info("收到中断，引擎退出")
    finally:
        login_ui.stop_server(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
