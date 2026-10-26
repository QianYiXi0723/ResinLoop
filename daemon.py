#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻守护进程 + 系统托盘。

为什么不再用 Windows 任务计划：
  - 小白配不明白，还得输登录密码；任务计划每次运行还会闪一个黑窗口；
  - 改提醒时间要重装任务。守护进程常驻后，网页里改完立即生效。

工作方式：
  - 每 30 秒醒一次，比对 ``config.json`` 的 ``app.check_times``；
  - 命中时（±1 分钟）跑一轮检查，结果写 ``logs/dailycheck.log``；
  - 每个时间点只在当天触发一次，记录在 ``logs/.last_run``；
  - 单次检查的任何异常都被吞掉写日志，进程不会退出；
  - 右下角托盘常驻：打开控制台 / 立即执行一次 / 退出程序。

资源占用：等待期间是 ``Event.wait``（等价 sleep），CPU 占用 0。

用法：
    python daemon.py                 # 常驻（带托盘）
    python daemon.py --no-tray       # 常驻（无托盘，调试用）
    python daemon.py --once          # 只跑一次就退出（排查问题用）
    python daemon.py --status        # 打印状态 JSON
    python daemon.py --start         # 后台拉起自己（用 pythonw）
    python daemon.py --stop          # 停止正在运行的实例
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket as _socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / "config.json"
LOGS_DIR = BASE_DIR / "logs"
PID_FILE = LOGS_DIR / "daemon.pid"
HEARTBEAT_FILE = LOGS_DIR / "daemon.heartbeat"
LAST_RUN_FILE = LOGS_DIR / ".last_run"
STOP_FLAG = LOGS_DIR / ".stop"
CONSOLE_PORT = 8765
CONSOLE_URL = f"http://127.0.0.1:{CONSOLE_PORT}/"

#: 轮询间隔（秒）
TICK_SECONDS = 30
#: 时间点容错（秒）：±1 分钟
SLOT_TOLERANCE = 60
#: 心跳超过这个秒数没更新，就认为进程已经不在了
HEARTBEAT_STALE = 120
#: .last_run 里最多保留多少条历史
LAST_RUN_KEEP = 40

#: Windows 进程创建标志
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

logger = logging.getLogger("dailycheck.daemon")


# --------------------------------------------------------------------------- #
# 目录与进程状态
# --------------------------------------------------------------------------- #


def ensure_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def pid_alive(pid: int) -> bool:
    """判断 PID 是否还活着。

    Windows 上不能用 ``os.kill(pid, 0)`` —— 它会把信号当成终止命令。
    这里走 OpenProcess + GetExitCodeProcess。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_pid_info() -> Optional[Dict[str, Any]]:
    """读 daemon.pid；兼容"纯数字"的旧格式。"""
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("pid"):
            return data
    except ValueError:
        pass
    try:
        return {"pid": int(text), "started_at": ""}
    except ValueError:
        return None


def read_pid() -> Optional[int]:
    info = read_pid_info()
    try:
        return int(info["pid"]) if info else None
    except (KeyError, TypeError, ValueError):
        return None


def write_pid() -> None:
    ensure_dirs()
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.executable,
    }
    PID_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def clear_state() -> None:
    """清掉 pid / 心跳 / 停止标记。"""
    for path in (PID_FILE, HEARTBEAT_FILE, STOP_FLAG):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def write_heartbeat() -> None:
    try:
        ensure_dirs()
        HEARTBEAT_FILE.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("写心跳失败（磁盘只读？）", exc_info=True)


def heartbeat_age() -> Optional[float]:
    try:
        return max(0.0, time.time() - HEARTBEAT_FILE.stat().st_mtime)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 配置读取（只读 app.check_times，避免拖慢轮询）
# --------------------------------------------------------------------------- #


def read_check_times() -> List[str]:
    """直接读 config.json 的时间点，改完无需重启即生效。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    app = raw.get("app") if isinstance(raw, dict) else None
    if not isinstance(app, dict):
        return []

    result: List[str] = []
    for item in app.get("check_times") or []:
        text = str(item).strip()
        try:
            hour, minute = (int(part) for part in text.split(":"))
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59 and text not in result:
            result.append(text)
    return sorted(result)


def next_run_text(check_times: Optional[List[str]] = None) -> str:
    from core.config import next_check_time

    times = check_times if check_times is not None else read_check_times()
    target = next_check_time(times)
    return target.strftime("%Y-%m-%d %H:%M") if target else ""


def status() -> Dict[str, Any]:
    """守护进程状态：给网页「定时与状态」用，也给 --status 用。"""
    info = read_pid_info() or {}
    pid = read_pid()
    alive = bool(pid and pid_alive(pid))
    age = heartbeat_age()
    running = alive and age is not None and age <= HEARTBEAT_STALE

    state = load_last_run()
    times = read_check_times()

    return {
        "running": running,
        "pid": pid,
        "alive": alive,
        "started_at": info.get("started_at", ""),
        "heartbeat_age": round(age, 1) if age is not None else None,
        "check_times": times,
        "next_run": next_run_text(times) if times else "",
        "last_run": {
            "slot": state.get("last_slot", ""),
            "at": state.get("last_at", ""),
            "exit": state.get("last_exit"),
        },
    }


# --------------------------------------------------------------------------- #
# 防重复触发
# --------------------------------------------------------------------------- #


def load_last_run() -> Dict[str, Any]:
    try:
        data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("fired"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"fired": {}}


def save_last_run(state: Dict[str, Any]) -> None:
    ensure_dirs()
    fired = state.setdefault("fired", {})
    if len(fired) > LAST_RUN_KEEP:
        keep = sorted(fired.items(), key=lambda kv: str(kv[1]))[-LAST_RUN_KEEP:]
        state["fired"] = dict(keep)

    tmp = LAST_RUN_FILE.with_name(LAST_RUN_FILE.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, LAST_RUN_FILE)
    except OSError:
        logger.warning("写 %s 失败，本轮防重复记录可能丢失", LAST_RUN_FILE.name)


def due_slots(
    now: Optional[datetime] = None,
    check_times: Optional[List[str]] = None,
    state: Optional[Dict[str, Any]] = None,
    tolerance: int = SLOT_TOLERANCE,
) -> List[str]:
    """返回当前应当触发、且今天还没触发过的时间点（形如 ``2026-10-26 08:30``）。"""
    current = now or datetime.now()
    times = check_times if check_times is not None else read_check_times()
    book = state if state is not None else load_last_run()
    fired = book.get("fired") or {}
    today = current.strftime("%Y-%m-%d")

    due: List[str] = []
    for text in times:
        try:
            hour, minute = (int(part) for part in text.split(":"))
        except ValueError:
            continue
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if abs((current - target).total_seconds()) > tolerance:
            continue
        key = f"{today} {text}"
        if key in fired:
            continue
        due.append(key)
    return due


# --------------------------------------------------------------------------- #
# 执行一轮检查
# --------------------------------------------------------------------------- #


def setup_logging(console: bool = False) -> None:
    """挂一次日志 handler（守护进程常驻，不重复挂）。"""
    from core.config import LoggingConfig
    from core.logger import setup_logger

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        logging_cfg = LoggingConfig.from_dict(config.get("logging"))
    except Exception:  # noqa: BLE001 - 配置坏了也要能写日志
        logging_cfg = LoggingConfig()
    setup_logger(logging_cfg, base_dir=BASE_DIR, console=console)


def run_check(reason: str, dry_run: bool = False) -> int:
    """跑一轮检查，任何异常都不许冒出去。返回退出码（失败也返回非 0）。"""
    try:
        from core.config import load_config
        from main import _apply_mihoyo_query_config, run_once
    except Exception:  # noqa: BLE001
        logger.exception("导入检查模块失败，跳过本轮")
        return 1

    try:
        config = load_config(CONFIG_PATH, auto_create=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("读取 config.json 失败，本轮跳过：%s", exc)
        return 1

    logger.info("=" * 56)
    logger.info(
        "开始检查｜触发方式=%s｜时间点=%s｜下次=%s",
        reason,
        "、".join(config.check_times),
        next_run_text(config.check_times),
    )
    try:
        _apply_mihoyo_query_config(config)
        code = run_once(config, dry_run=dry_run)
        logger.info("本轮结束，退出码=%d", code)
        return code
    except Exception:  # noqa: BLE001 - 断网 / 接口改版都不许弄死守护进程
        logger.exception("检查过程异常，守护进程继续运行")
        return 1


# --------------------------------------------------------------------------- #
# 主循环（跑在独立线程里，主线程留给 pystray）
# --------------------------------------------------------------------------- #


def _wait(seconds: float, stop_event: threading.Event) -> None:
    """可中断的休眠。

    本质仍是 sleep（CPU 0），只是拆成 1 秒一片：既能马上响应"停止"，
    也能让托盘里的"立即执行一次"不用干等 30 秒。
    """
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if STOP_FLAG.exists():
            logger.info("检测到停止标记 %s", STOP_FLAG.name)
            stop_event.set()
            return
        stop_event.wait(min(1.0, remaining))


def _tick(stop_event: threading.Event) -> None:
    if STOP_FLAG.exists():
        logger.info("检测到停止标记，准备退出")
        stop_event.set()
        return

    check_times = read_check_times()
    if not check_times:
        logger.warning("config.json 里没有可用的 app.check_times，本轮不调度")
        return

    state = load_last_run()
    due = due_slots(check_times=check_times, state=state)
    if not due:
        return

    for slot in due:
        # 先记账、后执行：即便执行途中进程被杀，也不会在下一轮重复推送
        state.setdefault("fired", {})[slot] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        save_last_run(state)
        logger.info("命中时间点 %s，开始检查", slot)

        code = run_check(f"定时 {slot}")
        state = load_last_run()
        state["last_slot"] = slot
        state["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_exit"] = code
        save_last_run(state)
        logger.info("时间点 %s 处理完毕（退出码 %d）", slot, code)

        if stop_event.is_set():
            return


def _loop(stop_event: threading.Event) -> None:
    logger.info("轮询线程已启动，间隔 %d 秒", TICK_SECONDS)
    while not stop_event.is_set():
        try:
            _tick(stop_event)
        except Exception:  # noqa: BLE001 - 兜底：循环自身出问题也不许死
            logger.exception("轮询异常（已忽略，继续运行）")
        write_heartbeat()
        _wait(TICK_SECONDS, stop_event)
    logger.info("轮询线程已退出")


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #


def _tray_image():
    """画一个 64x64 的图标（不依赖外部图片文件）。"""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([2, 2, 61, 61], radius=15, fill=(37, 99, 235, 255))
    draw.line([(16, 34), (27, 46), (48, 19)], fill=(255, 255, 255, 255), width=7,
              joint="curve")
    return image


def _console_running() -> bool:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", CONSOLE_PORT)) == 0


def _open_console() -> None:
    try:
        if not _console_running():
            logger.info("控制台没在运行，顺手拉起 login_ui.py")
            spawn([str(pythonw_exe()), str(BASE_DIR / "login_ui.py")])
            time.sleep(2.0)
        import webbrowser

        webbrowser.open(CONSOLE_URL)
    except Exception:  # noqa: BLE001
        logger.exception("打开控制台失败")


def _run_tray(stop_event: threading.Event, on_ready: Any = None) -> None:
    """阻塞在 pystray 的消息循环里（必须在主线程调用）。

    ``on_ready(icon)`` 在进入循环前回调，用于把 icon 交给监视线程——
    否则 stop_event 被置位时主线程仍卡在 icon.run() 里，进程退不掉。
    """
    import pystray

    def on_open(icon: Any, item: Any) -> None:  # noqa: ARG001
        threading.Thread(target=_open_console, daemon=True).start()

    def on_run(icon: Any, item: Any) -> None:  # noqa: ARG001
        threading.Thread(
            target=lambda: run_check("托盘手动触发"), name="manual-run", daemon=True
        ).start()

    def on_quit(icon: Any, item: Any) -> None:  # noqa: ARG001
        logger.info("托盘选择了退出")
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台", on_open, default=True),
        pystray.MenuItem("立即执行一次", on_run),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出程序", on_quit),
    )
    icon = pystray.Icon("gamedaily", _tray_image(), "游戏日常提醒", menu)
    logger.info("托盘图标已创建")
    if on_ready is not None:
        on_ready(icon)
    icon.run()


# --------------------------------------------------------------------------- #
# 进程控制（网页「启动 / 停止」按钮走这里）
# --------------------------------------------------------------------------- #


def pythonw_exe() -> Path:
    """优先用 pythonw.exe：无控制台窗口，双击/后台拉起都不闪黑框。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def spawn(args: List[str]) -> subprocess.Popen:
    """脱离父进程地拉起一个后台进程。"""
    flags = 0
    if os.name == "nt":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    return subprocess.Popen(
        args,
        cwd=str(BASE_DIR),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def start_daemon() -> Dict[str, Any]:
    current = status()
    if current["running"]:
        return {
            "ok": True,
            "already": True,
            "pid": current["pid"],
            "message": f"守护进程已在运行（PID {current['pid']}）",
        }

    # 清掉上次异常退出留下的 pid / 心跳，避免误判
    clear_state()
    try:
        proc = spawn([str(pythonw_exe()), str(BASE_DIR / "daemon.py")])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"拉起失败：{exc}"}

    for _ in range(24):  # 最多等 6 秒
        time.sleep(0.25)
        current = status()
        if current["running"]:
            return {
                "ok": True,
                "pid": current["pid"],
                "message": f"守护进程已启动（PID {current['pid']}）",
            }
        if proc.poll() is not None:
            return {
                "ok": False,
                "message": (
                    f"守护进程启动后立即退出（code={proc.returncode}），"
                    "请查看 logs/dailycheck.log"
                ),
            }

    return {"ok": False, "message": "已拉起，但 6 秒内没有心跳，请稍后刷新状态"}


def stop_daemon(timeout: float = 8.0) -> Dict[str, Any]:
    """先温和退出，超时再强杀。返回结果里带 forced 标记。"""
    pid = read_pid()
    if not pid or not pid_alive(pid):
        clear_state()
        return {"ok": True, "already": True, "message": "守护进程未在运行"}

    ensure_dirs()
    try:
        STOP_FLAG.write_text(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8"
        )
    except OSError:
        logger.warning("写停止标记失败，将直接强杀")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.25)

    forced = False
    if pid_alive(pid):
        logger.info("守护进程未在 %.0f 秒内退出，强制结束 PID %s", timeout, pid)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("taskkill 调用失败")
        forced = True
        for _ in range(25):
            if not pid_alive(pid):
                break
            time.sleep(0.2)

    alive = pid_alive(pid)
    clear_state()
    if alive:
        return {
            "ok": False,
            "pid": pid,
            "message": f"无法结束 PID {pid}，请在任务管理器里手动结束后重试",
        }
    return {
        "ok": True,
        "pid": pid,
        "forced": forced,
        "message": "已强制结束（来不及正常退出）" if forced else "已停止，内存已释放",
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def run_daemon(use_tray: bool = True) -> int:
    ensure_dirs()
    current = status()
    if current["running"]:
        print(f"守护进程已在运行（PID {current['pid']}），本次不重复启动")
        return 0

    clear_state()
    write_pid()
    setup_logging(console=not use_tray)

    logger.info("=" * 56)
    logger.info("守护进程启动｜PID=%s｜间隔=%ds", os.getpid(), TICK_SECONDS)
    logger.info("时间点：%s｜下次：%s", "、".join(read_check_times()) or "(未配置)",
                next_run_text() or "-")
    logger.info("日志：%s", LOGS_DIR / "dailycheck.log")

    stop_event = threading.Event()
    worker = threading.Thread(target=_loop, args=(stop_event,), name="check-loop",
                              daemon=True)
    worker.start()

    write_heartbeat()

    if use_tray:
        def _stop_icon_when_signalled(icon: Any) -> None:
            """监视线程：收到停止信号就收起托盘，让主线程的 icon.run() 返回。"""
            stop_event.wait()
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                logger.debug("收起托盘图标失败（忽略）", exc_info=True)

        def _hand_over(icon: Any) -> None:
            threading.Thread(
                target=_stop_icon_when_signalled,
                args=(icon,),
                name="tray-watchdog",
                daemon=True,
            ).start()

        try:
            _run_tray(stop_event, on_ready=_hand_over)
        except ImportError:
            logger.warning("未安装 pystray / Pillow，无法显示托盘，转为无界面常驻")
            use_tray = False
        except Exception:  # noqa: BLE001
            logger.exception("托盘启动失败，转为无界面常驻")
            use_tray = False
        else:
            # icon.run() 正常返回只有一种情况：用户点了"退出程序"，或监视线程收起了图标。
            # 若它意外返回却没置位 stop_event，说明托盘环境不可用 —— 不能就此退出，
            # 退回无界面常驻，否则定时提醒会悄悄消失。
            if not stop_event.is_set():
                logger.warning("托盘循环意外返回，转为无界面常驻")
                use_tray = False

    if not use_tray:
        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C")
            stop_event.set()

    stop_event.set()
    worker.join(timeout=5)
    clear_state()
    logger.info("守护进程已退出")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="daemon.py",
        description="游戏日常提醒 · 常驻守护进程（托盘 + 定时检查）",
    )
    parser.add_argument("--once", action="store_true", help="只执行一次检查后退出")
    parser.add_argument(
        "--dry-run", action="store_true", help="配合 --once：跑完整流程但不真的发推送"
    )
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘，前台常驻")
    parser.add_argument("--status", action="store_true", help="打印状态 JSON")
    parser.add_argument("--start", action="store_true", help="后台拉起守护进程")
    parser.add_argument("--stop", action="store_true", help="停止守护进程")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if args.start:
        result = start_daemon()
        print(result["message"])
        return 0 if result.get("ok") else 1

    if args.stop:
        result = stop_daemon()
        print(result["message"])
        return 0 if result.get("ok") else 1

    if args.once:
        ensure_dirs()
        setup_logging(console=True)
        code = run_check("命令行 --once", dry_run=args.dry_run)
        return code

    return run_daemon(use_tray=not args.no_tray)


if __name__ == "__main__":
    sys.exit(main())
