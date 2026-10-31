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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / "config.json"
LOGS_DIR = BASE_DIR / "logs"
PID_FILE = LOGS_DIR / "daemon.pid"
HEARTBEAT_FILE = LOGS_DIR / "daemon.heartbeat"
RUN_NOW_FLAG = LOGS_DIR / ".run_now"
LAST_RUN_FILE = LOGS_DIR / ".last_run"
STOP_FLAG = LOGS_DIR / ".stop"
CONSOLE_PORT = 8765
CONSOLE_URL = f"http://127.0.0.1:{CONSOLE_PORT}/"

#: 轮询间隔（秒）
TICK_SECONDS = 30
#: 时间点容错（秒）：±1 分钟
SLOT_TOLERANCE = 60
#: 错过时间点后的补发窗口（分钟）：0 = 关闭补发
CATCH_UP_MINUTES_DEFAULT = 180
CATCH_UP_MINUTES_MAX = 720
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


def _read_app_section() -> Dict[str, Any]:
    """直接读 config.json 的 app 段（不经过完整解析，避免 30 秒一次的开销）。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    app = raw.get("app") if isinstance(raw, dict) else None
    return app if isinstance(app, dict) else {}


def read_check_times() -> List[str]:
    """直接读 config.json 的时间点，改完无需重启即生效。"""
    result: List[str] = []
    for item in _read_app_section().get("check_times") or []:
        text = str(item).strip()
        try:
            hour, minute = (int(part) for part in text.split(":"))
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59 and text not in result:
            result.append(text)
    return sorted(result)


def read_catch_up_enabled() -> bool:
    """补发总开关：没配这个字段时按开启处理（兼容老配置）。

    关掉后 catch_up_minutes 的数值原样保留，重新打开即恢复窗口。
    """
    value = _read_app_section().get("catch_up_enabled")
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "关", "关闭")


def read_catch_up_minutes() -> int:
    """补发窗口（分钟）：休眠 / 关机错过时间点后，醒来立刻补跑一次。

    0 或负数 = 关闭补发；没配这个字段时按默认 180 分钟处理。
    总开关 catch_up_enabled 为 false 时直接返回 0，补发逻辑自然整体失效。
    """
    if not read_catch_up_enabled():
        return 0
    try:
        value = int(_read_app_section().get("catch_up_minutes"))
    except (TypeError, ValueError):
        return CATCH_UP_MINUTES_DEFAULT
    if value < 0:
        return 0
    return min(value, CATCH_UP_MINUTES_MAX)


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
        "catch_up_minutes": read_catch_up_minutes(),
        "catch_up_enabled": read_catch_up_enabled(),
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


def pending_slots(
    now: Optional[datetime] = None,
    check_times: Optional[List[str]] = None,
    state: Optional[Dict[str, Any]] = None,
    tolerance: int = SLOT_TOLERANCE,
    catch_up_minutes: Optional[int] = None,
) -> List[Tuple[str, int]]:
    """列出该触发的时间点，返回 ``[(slot_key, 迟到秒数), ...]``。

    迟到秒数 = 当前时间 - 目标时间：
      * 落在 ±``tolerance`` 内 → 正常到点（迟到约 0）
      * 超出容差但仍在 ``catch_up_minutes`` 窗口内 → 补发（合盖睡过去醒来的情况）
      * 超过补发窗口 → 当天不再触发

    跨 0 点补发：23:59 这类深夜班次错过后的补发窗口会延续到第二天凌晨
    （23:59 + 180 分钟 = 次日 02:59）。目标时刻按"昨天同刻"重建并计入
    昨天的槽位键，凌晨补跑的检查经 history.record 的游戏日期（04:00 日界）
    自然归属昨天 —— 槽位键与数据归属两边口径一致。
    """
    current = now or datetime.now()
    times = check_times if check_times is not None else read_check_times()
    book = state if state is not None else load_last_run()
    fired = book.get("fired") or {}
    today = current.strftime("%Y-%m-%d")

    if catch_up_minutes is None:
        catch_up_minutes = read_catch_up_minutes()
    catch_up_seconds = max(int(catch_up_minutes), 0) * 60

    pending: List[Tuple[str, int]] = []
    for text in times:
        try:
            hour, minute = (int(part) for part in text.split(":"))
        except ValueError:
            continue
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = int((current - target).total_seconds())

        if abs(delta) <= tolerance:
            # 正常到点（含 ±tolerance 内的轻微抖动）
            key = f"{today} {text}"
            if key not in fired:
                pending.append((key, delta))
            continue

        if 0 < delta <= catch_up_seconds:
            # 睡过头了：今天的班，只在补发窗口内、且还没跑过时才补一次
            key = f"{today} {text}"
            if key not in fired:
                pending.append((key, delta))
            continue

        # 跨 0 点的补发窗口：昨天的这一班还在窗口里（比如 00:30 补 23:59）
        y_target = target - timedelta(days=1)
        y_delta = int((current - y_target).total_seconds())
        if 0 < y_delta <= catch_up_seconds:
            key = f"{y_target.strftime('%Y-%m-%d')} {text}"
            if key not in fired:
                pending.append((key, y_delta))
    return pending


def due_slots(
    now: Optional[datetime] = None,
    check_times: Optional[List[str]] = None,
    state: Optional[Dict[str, Any]] = None,
    tolerance: int = SLOT_TOLERANCE,
    catch_up_minutes: Optional[int] = None,
) -> List[str]:
    """返回当前应当触发、且今天还没触发过的时间点（形如 ``2026-10-26 08:30``）。"""
    return [
        key
        for key, _delta in pending_slots(
            now=now,
            check_times=check_times,
            state=state,
            tolerance=tolerance,
            catch_up_minutes=catch_up_minutes,
        )
    ]


# --------------------------------------------------------------------------- #
# 每周报告（每周日 21:00 后发送，常开无开关；错过自动补发）
# --------------------------------------------------------------------------- #

WEEKLY_STATE_FILE = LOGS_DIR / ".last_weekly"


def weekly_slot_week(now: datetime) -> str:
    """最近一个已过去的「周日 21:00」槽位所属的 ISO 周键（如 "2026-W44"）。

    周日 21:00 之前指的是上一周的槽位（那时该补发的是上周报告），
    21:00 起切换到本周。纯函数，rl_test_weekly 覆盖边界。
    """
    last_sunday = now.date() - timedelta(days=(now.weekday() - 6) % 7)
    slot = datetime.combine(last_sunday, datetime.min.time()).replace(hour=21)
    if now < slot:
        last_sunday -= timedelta(days=7)
    iso = last_sunday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_due(now: datetime, state: Optional[Dict[str, Any]] = None) -> bool:
    """本周槽位还没发过周报 → True。"""
    return (state or {}).get("week") != weekly_slot_week(now)


def load_weekly_state() -> Dict[str, Any]:
    try:
        data = json.loads(WEEKLY_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 状态丢了顶多发一次，不影响主流程
        return {}


def save_weekly_state(state: Dict[str, Any]) -> None:
    """原子落盘；失败只影响去重，不影响守护进程。"""
    try:
        WEEKLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WEEKLY_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(WEEKLY_STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写周报状态失败（下周可能重发一次）：%s", exc)


def format_weekly_body(digest: Dict[str, Any]) -> str:
    """周报正文（纯函数）：「本周：原神 7 天全勤，星铁完成 5 天、漏做 1 次…」"""
    parts: List[str] = []
    total_days = int(digest.get("days_count", 7))
    for game in digest.get("games") or []:
        if int(game.get("known_days", 0)) <= 0:
            continue  # 整周没查到数据的游戏不进周报
        name = str(game.get("name") or game.get("game") or "?")
        done = int(game.get("done_days", 0))
        missed = int(game.get("missed_days", 0))
        if done >= total_days and missed == 0:
            parts.append(f"{name} {done} 天全勤")
        elif missed > 0:
            parts.append(f"{name} 完成 {done} 天、漏做 {missed} 次")
        else:
            parts.append(f"{name} 完成 {done} 天")
    if not parts:
        return ""
    missed_any = any(
        int(g.get("missed_days", 0)) > 0 for g in (digest.get("games") or [])
    )
    tail = "有漏做，下周继续加油！" if missed_any else "全部完成，可以安心摆烂了。"
    return "本周：" + "，".join(parts) + "。" + tail


def _has_bound_credential(config) -> bool:
    """启用账号里是否存在任何真实凭证（cookie/token 任一非空）。

    mock 门卫的事实依据：provider 字段可能因旧版升级/手写配置停在 mock，
    但凭证是扫码/粘贴时写入的硬事实——有凭证就该真实查询，没有才是出厂态。
    """
    for acc in getattr(config, "enabled_accounts", None) or []:
        cred = getattr(acc, "credential", None) or {}
        if (str(cred.get("cookie") or "").strip()
                or str(cred.get("token") or "").strip()):
            return True
    return False


def run_weekly_report() -> bool:
    """组装并发送周报；返回是否有通道真正发成功。任何异常都吞掉写日志。"""
    try:
        from core.config import load_config
        from core import history
        from core.notifier import build_notifiers, push_all

        config = load_config(CONFIG_PATH, auto_create=False)
    except Exception:  # noqa: BLE001
        logger.exception("周报：读取配置失败，本轮跳过")
        return False
    # 与 run_check 同一门卫：出厂 mock 状态下历史里就算有残留数据也是假的，
    # 不该拿去发周报（2026-10-30 实测 mock 假数据被编进周报推出去了）。
    # 2026-10-31 朋友机器实证：provider 字段可能停在 mock 而凭证已经写入
    # （旧版升级残留等）——门卫升级为看凭证事实自愈，不再一刀切空转。
    if (config.query.provider or "mock").strip().lower() == "mock":
        if not _has_bound_credential(config):
            logger.info("周报：尚未绑定任何账号（provider=mock），跳过")
            return False
        logger.warning(
            "周报：provider=mock 但启用账号已有凭证——本轮按 miyoushe 继续"
            "（配置字段疑似未随扫码切换，重扫码或手改 config 可消除本警告）"
        )
        config.query.provider = "miyoushe"
    try:
        digest = history.weekly_digest(days=7)
        body = format_weekly_body(digest)
        if not body:
            logger.info("周报：过去 7 天没有任何已检查数据，跳过发送")
            return False
        notifiers = build_notifiers(config.push)
        if not notifiers:
            logger.warning("周报：没有可用推送通道（push.channels 为空或 Key 未配置），跳过")
            return False
        results = push_all(notifiers, "📊 ResinLoop 周报", body, dry_run=False)
        ok_count = sum(1 for r in results if r.ok)
        logger.info("周报已发送（%d/%d 通道成功）：%s", ok_count, len(results), body)
        return ok_count > 0
    except Exception:  # noqa: BLE001 - 周报失败绝不弄死守护进程
        logger.exception("周报发送失败")
        return False


def _maybe_weekly_report() -> None:
    """周报推送入口（_loop 每 30 秒调一次，内部自判周）。

    与每日槽位「先记账、后执行」同一哲学：无论发送成败都落 state，
    防止每 30 秒重试轰炸推送通道；失败详情在日志里，下周槽位自动再试。
    """
    try:
        now = datetime.now()
        state = load_weekly_state()
        if not weekly_due(now, state):
            return
        week_key = weekly_slot_week(now)
        sent = run_weekly_report()
        save_weekly_state({
            "week": week_key,
            "sent_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(sent),
        })
    except Exception:  # noqa: BLE001 - 兜底：循环自身出问题也不许死
        logger.exception("周报流程异常（已忽略，继续运行）")


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


#: 一轮检查的进程内重入门闩：调度 / 补发 / 托盘手动 / .run_now 四个线程入口都可能
#: 同时进来（一轮检查 30~90 秒，托盘按钮随时可点）。非阻塞获取——上一轮没跑完就
#: 跳过本次触发：定时槽在 _tick 里「先记账后执行」，跳过不会造成重复触发，而当轮
#: 在跑的那次本身就是完整检查（提醒决策看推送状态不看槽位）；手动触发再点一次即可。
_CHECK_GATE = threading.Lock()


def run_check(reason: str, dry_run: bool = False) -> int:
    """跑一轮检查，任何异常都不许冒出去。返回退出码（失败也返回非 0）。

    进程内重入保护见 _CHECK_GATE；CLI --once 是独立进程，不在此闸内。
    """
    if not _CHECK_GATE.acquire(blocking=False):
        logger.warning("上一轮检查尚未结束，本次触发跳过：%s", reason)
        return 0
    try:
        return _run_check_locked(reason, dry_run=dry_run)
    finally:
        _CHECK_GATE.release()


def _run_check_locked(reason: str, dry_run: bool = False) -> int:
    """run_check 的实体（调用方必须已持 _CHECK_GATE）。"""
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

    # 出厂状态（provider=mock）：查出来的全是写死的演示数据。
    # 2026-10-30 实测：新机器首启补跑 20:30 那轮，mock 假数据被写进巡检历史、
    # 还编进周报推了出去（「崩坏：星穹铁道 1 天全勤」——用户根本没绑过号）。
    # 所以出厂态直接跳过：不查询、不写历史、不推送。返回 0 让补发机制把
    # 该时间点记为已处理，避免 30 秒一轮地反复触发。
    # 2026-10-31 朋友机器实证（第二轮护栏）：provider 字段可能停在 mock 而
    # 凭证已经写入（旧版升级残留/手写配置）——原门卫一刀切跳过会让这台机器
    # 永远空转（游戏概览/周报/巡检全空）。升级为看凭证事实自愈：有凭证就按
    # miyoushe 继续本轮（米游社系直连、森空岛系走回退链），大声警告；真正的
    # 出厂态（无任何凭证）维持跳过。provider 字段的持久化修正交给重新扫码。
    if (config.query.provider or "mock").strip().lower() == "mock":
        if not _has_bound_credential(config):
            logger.info("尚未绑定任何账号（provider=mock），本轮巡检跳过：不查询、不写历史、不推送")
            return 0
        logger.warning(
            "provider=mock 但启用账号已有凭证——本轮自愈为 miyoushe 继续巡检"
            "（配置字段疑似未随扫码切换，重扫码或手改 config 可消除本警告）"
        )
        config.query.provider = "miyoushe"

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


def _wait(seconds: float, stop_event: threading.Event) -> bool:
    """可中断的休眠。

    返回 True = 检测到 .run_now 手动巡检标记（调用方应立即跑一轮）；
    返回 False = 正常到点 / stop_event 被置位 / STOP_FLAG。
    本质仍是 sleep（CPU 0），拆成 0.5 秒一片：既能马上响应"停止"，
    也能让托盘里的"立即巡检"不用干等 30 秒。
    """
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if STOP_FLAG.exists():
            logger.info("检测到停止标记 %s", STOP_FLAG.name)
            stop_event.set()
            return False
        stop_event.wait(min(0.5, remaining))
        if RUN_NOW_FLAG.exists():
            try:
                RUN_NOW_FLAG.unlink()
            except OSError as exc:
                # 删不掉就跳过本轮触发（下个 tick 再试）——照常触发的话标记残留
                # 会以「30 秒 + 一轮检查」的节奏无限重复手动巡检
                logger.warning("手动巡检标记删除失败（%s），本轮跳过等待下个 tick 重试", exc)
                continue
            logger.info("收到立即巡检请求 %s", RUN_NOW_FLAG.name)
            return True
    return False


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
    pending = pending_slots(check_times=check_times, state=state)
    if not pending:
        return

    for slot, late_seconds in pending:
        # 先记账、后执行：即便执行途中进程被杀，也不会在下一轮重复推送
        state.setdefault("fired", {})[slot] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        save_last_run(state)
        if abs(late_seconds) > SLOT_TOLERANCE:
            logger.info(
                "错过了时间点 %s（迟到 %.0f 分钟），现在补发一次检查",
                slot,
                late_seconds / 60.0,
            )
        else:
            logger.info("命中时间点 %s，开始检查", slot)

        code = run_check(f"补发 {slot}" if abs(late_seconds) > SLOT_TOLERANCE else f"定时 {slot}")
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
        _maybe_weekly_report()   # 周日 21:00 后的周度推送（常开，错过自动补发）
        if _wait(TICK_SECONDS, stop_event):
            run_check("手动")
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
    """托盘「打开控制台」：优先拉起桌面窗口（app.py），失败才退回浏览器。

    以前是起 login_ui.py + 开浏览器；现在双击/托盘都统一到独立窗口，
    免得用户每次还要在浏览器里找标签页。
    """
    try:
        desktop = BASE_DIR / "app.py"
        if desktop.exists():
            logger.info("拉起桌面控制台 app.py")
            spawn([str(pythonw_exe()), str(desktop)], gui=True)
            return

        if not _console_running():
            logger.info("桌面入口不存在，退回拉起 login_ui.py")
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
    icon = pystray.Icon("resinloop", _tray_image(), "ResinLoop · 日常巡查", menu)
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


def spawn(args: List[str], *, gui: bool = False) -> subprocess.Popen:
    """脱离父进程地拉起一个后台进程。

    ``gui=True`` 用于有窗口的程序（app.py）：**不能**带 ``CREATE_NO_WINDOW``。
    那个标志会往 STARTUPINFO 里塞 ``SW_HIDE``，导致 GUI 的首个窗口被隐藏 ——
    表现为进程起来了、端口也在听，但屏幕上看不到窗口（实测踩过）。
    """
    flags = 0
    if os.name == "nt":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        if not gui:
            flags |= _CREATE_NO_WINDOW
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

    # 心跳超时 ≠ 进程没了：一轮慢检查（活动缓存首拉 + 五游戏查询，最坏两分钟上下）
    # 就能让 status() 显示未运行。此时清状态拉新的会造出双守护进程（同槽位双推送、
    # 旧实例从此 UI 停不掉）——先验旧 pid 指向的进程是否真活着，活着就拒绝拉起。
    old_pid = current.get("pid")
    if old_pid and pid_alive(old_pid):
        return {
            "ok": False,
            "message": (
                f"检测到守护进程仍在运行（PID {old_pid}，心跳超时可能只是检查耗时长），"
                "已拒绝重复拉起；确认卡死请先在托盘退出或稍后再试"
            ),
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
    # 单实例终极防线：对 daemon.lock 取非阻塞排它锁并持有到进程死亡——
    # 内核保证被杀时自动释放，不存在陈旧锁（2026-10-31 审计：原「status 检查 →
    # clear_state → write_pid」两步之间存在 TOCTOU，并发拉起可双实例同槽位双推送）。
    # pid/心跳检查保留为 UX 快速通道，正确性以这把锁为准。
    from core.ipc_lock import try_acquire

    daemon_lock_fd = try_acquire(str(BASE_DIR / "logs" / "daemon.lock"))
    if daemon_lock_fd is None:
        print("另一个守护进程正在运行（daemon.lock 被其它进程持有），本次不重复启动")
        return 0
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
    catch_up = read_catch_up_minutes()
    if not read_catch_up_enabled():
        logger.info("补发窗口：已被总开关关闭（catch_up_enabled = false）")
    else:
        logger.info(
            "补发窗口：%s", f"{catch_up} 分钟" if catch_up > 0 else "已关闭（错过就不再补）"
        )
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
        description="ResinLoop · 常驻守护进程（托盘 + 定时检查）",
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
