"""日志模块。

职责：初始化统一 logger —— 控制台 + 按天切分的文件日志（logs/dailycheck.log，
零点切分成 dailycheck.log.YYYY-MM-DD，保留 keep_days 天）。所有模块通过
logging.getLogger("dailycheck.xxx") 取子 logger，handler 只在 main 里挂一次。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

ROOT_LOGGER_NAME = "dailycheck"
FILE_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
CONSOLE_FORMAT = "%(asctime)s [%(levelname)-7s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(
    config: LoggingConfig,
    base_dir: Path | None = None,
    console: bool = True,
) -> logging.Logger:
    """构建并返回根 logger（可重复调用，会先清理旧 handler 避免重复输出）。

    Args:
        config: 日志配置（目录 / 级别 / 保留天数）。
        base_dir: 相对路径的基准目录，一般传项目根目录。
        console: 是否同时输出到控制台。
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(_resolve_level(config.level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(FILE_FORMAT, DATE_FORMAT)
    log_dir = Path(config.dir).expanduser()
    if not log_dir.is_absolute() and base_dir is not None:
        log_dir = Path(base_dir) / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=log_dir / "dailycheck.log",
        when="midnight",
        backupCount=max(int(config.keep_days), 1),
        encoding="utf-8",
        delay=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
        stream_handler.setLevel(_resolve_level(config.level))
        logger.addHandler(stream_handler)

    return logger


def _resolve_level(level: str) -> int:
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO
