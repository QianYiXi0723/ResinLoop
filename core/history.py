# -*- coding: utf-8 -*-
"""每日完成情况存档（data/history.json）。

为什么是 JSON 而不是 sqlite：
    一天最多几十条记录、字段固定、只在定时检查时写一次，
    用 sqlite 属于杀鸡用牛刀，还多一个文件锁的麻烦。JSON 直接看、
    直接改、坏了不丢配置。

设计要点（**不增加任何额外请求**）：
    只在 main.run_once() 里、本轮查询结束后顺手写一次，
    数据全部来自已有的 DailyReport，绝不为了统计去查一次 API。

一天的口径：
    同一天可能检查多次（08:30 / 20:30 / 手动跑），
    只要**任意一次**查到"全部完成"，这一天就算完成 —— 日常当天补完也算。
    查询失败的那次不算"未完成"（避免把网络抖动记成漏做），只计入 known。

    「一天」按**游戏日期**算：游戏日常在凌晨 04:00 刷新，所以逻辑日期
    以 04:00 为界 —— 真实时刻 00:00~03:59 的检查/补发归属前一天
    （见 :func:`game_moment` / :func:`game_date_key`）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import KNOWN_GAMES

logger = logging.getLogger("dailycheck.history")

#: 存档根目录：<项目>/data/history.json
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HISTORY_PATH = DATA_DIR / "history.json"

#: 上一轮巡检完整快照的文件名（推送预览 /api/push_preview 的离线数据源）。
#: 与 history.json 分文件：这里存任务明细与 stale 原因（逐字重建推送正文），
#: history 只存计数（报表/热力图消费）。落盘/读取一律以 DATA_DIR 现场拼接，
#: 测试重定向 DATA_DIR 即可整体隔离（勿改成模块加载期的绝对常量）。
REPORT_SNAPSHOT_NAME = "last_report.json"

#: 最多保留多少天（按日期清理，一天一条聚合，180 天也就几 KB）
KEEP_DAYS = 180

#: 报表默认展示天数
DEFAULT_DAYS = 7

#: 游戏日常在凌晨 04:00 刷新：逻辑日期（Game Date）以 04:00 为界。
#: 真实时刻 00:00~03:59 之间发生的一切（检查、补发、手动跑）都归属"昨天"。
GAME_DAY_OFFSET = timedelta(hours=4)

#: 列的固定顺序（没见过的游戏排在后面）
GAME_ORDER = ("genshin", "starrail", "zzz", "arknights", "endfield")

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# --------------------------------------------------------------------------- #
# 游戏日期（04:00 日界）
# --------------------------------------------------------------------------- #


def game_moment(now: Optional[datetime] = None) -> datetime:
    """游戏逻辑时刻 = 真实时刻 - 4 小时；取 ``.date()`` 即逻辑日期。

    全项目所有"今天算哪天"的判定都必须走这里，禁止各处自行 strftime。
    """
    return (now or datetime.now()) - GAME_DAY_OFFSET


def game_date_key(now: Optional[datetime] = None) -> str:
    """当前的游戏日期（YYYY-MM-DD）：凌晨 04:00 前算前一天。"""
    return game_moment(now).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def _empty() -> Dict[str, Any]:
    return {"version": 1, "days": {}}


def load() -> Dict[str, Any]:
    """读存档；**任何**异常都返回空结构，绝不向上抛。

    这个函数被 /api/history（网页报表）和 record()（每轮检查）调用，
    一旦抛异常：前者会让报表页弹「无法读取历史记录」，后者会拖垮整轮检查。
    所以这里的策略是"宁可显示空报表，也不能挂"：

        文件不存在        -> 空结构（首次运行，正常情况）
        没有读权限 / 被占用 -> 空结构 + WARNING
        不是 UTF-8 字节    -> 空结构 + WARNING（UnicodeDecodeError 不是 OSError！）
        JSON 语法坏       -> 空结构 + WARNING
        顶层结构不对       -> 空结构
        单天/单游戏节点坏   -> 丢掉坏节点，其余照常展示（尽量救回可用数据）
    """
    try:
        raw = HISTORY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        logger.warning("读取历史记录失败（%s），本次按空处理", exc)
        return _empty()
    except UnicodeDecodeError as exc:
        # 文件被别的编辑器存成了 GBK / 二进制：解码就炸，读都读不到
        logger.warning("历史记录不是 UTF-8 编码（%s），本次按空处理", exc)
        return _empty()
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不把报表页搞挂
        logger.warning("读取历史记录时出现未预期错误（%s），本次按空处理", exc)
        return _empty()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("历史记录 JSON 损坏（%s），本次按空处理", exc)
        return _empty()
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析历史记录时出现未预期错误（%s），本次按空处理", exc)
        return _empty()

    if not isinstance(data, dict):
        logger.warning("历史记录顶层不是对象（%s），本次按空处理", type(data).__name__)
        return _empty()

    days = data.get("days")
    if not isinstance(days, dict):
        logger.warning("历史记录的 days 字段不是对象，本次按空处理")
        return _empty()

    # 清洗：某一天的值必须是 dict，某游戏节点也必须是 dict，
    # 否则后面 .items() / .get() 会 AttributeError —— 那才是 500 的根源。
    cleaned: Dict[str, Any] = {}
    for date_key, day in days.items():
        if not isinstance(date_key, str) or not isinstance(day, dict):
            continue
        cleaned[date_key] = {
            game: node for game, node in day.items() if isinstance(node, dict)
        }
    data["days"] = cleaned
    return data


def empty_payload(days: int = DEFAULT_DAYS, *, today: Optional[datetime] = None) -> Dict[str, Any]:
    """与 :func:`summary` 返回结构完全一致的空报表。

    /api/history 在异常兜底时用它 —— 前端不需要为"出错"写第二套渲染逻辑，
    照常渲染就是一张全 `–` 的空表。
    """
    days = max(1, min(int(days or DEFAULT_DAYS), 60))
    now = today or game_moment()   # 报表口径 = 游戏日期（04:00 日界）
    today_key = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    recent: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        cursor = now - timedelta(days=offset)
        key = cursor.strftime("%Y-%m-%d")
        recent.append({
            "date": key,
            "label": f"{int(key[5:7])}/{int(key[8:10])}",
            "weekday": _WEEKDAYS[cursor.weekday()],
            "is_today": key == today_key,
            "games": {},
            "all_done": False,
        })
    return {
        "ok": True,
        "path": str(HISTORY_PATH),
        "has_data": False,
        "today": today_key,
        "month": month,
        "days_count": days,
        "days": recent,
        "games": [],
        "tracked": [],
        "empty_reason": "还没有任何历史记录",
    }


def _atomic_write(data: Dict[str, Any]) -> None:
    """原子写：临时文件 → 复检 → os.replace，避免写一半断电留下坏文件。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    json.loads(text)  # 复检：序列化出来的必须是合法 JSON

    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".history-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
        os.replace(tmp, HISTORY_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(days: Dict[str, Any], keep: int = KEEP_DAYS) -> Dict[str, Any]:
    """只保留最近 keep 天。"""
    if len(days) <= keep:
        return days
    for key in sorted(days.keys())[:-keep]:
        days.pop(key, None)
    return days


def record(report: Any, *, at: Optional[datetime] = None) -> Dict[str, Any]:
    """把一轮检查结果并入存档，返回写入后的完整数据。

    Args:
        report: ``core.analyzer.DailyReport``（这里不 import，避免循环依赖）。
        at: 归属时间，默认用 report.generated_at。

    Returns:
        写入后的 dict；没有任何状态时原样返回未写盘的数据。
    """
    statuses = list(getattr(report, "statuses", []) or [])
    if not statuses:
        return load()

    moment = at or getattr(report, "generated_at", None) or datetime.now()
    date_key = game_date_key(moment)   # 04:00 日界：凌晨跑的检查归属昨天
    time_text = moment.strftime("%H:%M")

    data = load()
    days = data.setdefault("days", {})
    day = days.setdefault(date_key, {})

    for status in statuses:
        game = getattr(status, "game_id", "") or ""
        if not game:
            continue
        node = day.setdefault(game, {
            "game_name": getattr(status, "game_name", game),
            "nickname": getattr(status, "nickname", ""),
            "finished": False,
            "known": False,
            "checks": 0,
            "last": "",
            "unfinished_tasks": 0,
        })
        node["game_name"] = getattr(status, "game_name", game) or node.get("game_name", game)
        node["nickname"] = getattr(status, "nickname", "") or node.get("nickname", "")
        node["checks"] = int(node.get("checks") or 0) + 1
        node["last"] = time_text

        # 数据待更新（stale）与未创建角色（no_account）都等于"这次没拿到当日数据"：
        # 只刷新检查时间，不能把 known 置真 —— 否则报表会把"没登录/没建号"
        # 画成"已知、漏 0 项"。
        ok = bool(getattr(status, "ok", False)) and not getattr(
            status, "stale", None
        ) and not getattr(status, "no_account", None)
        if ok:
            # 完成是"粘"的：当天只要有一次查到全做完，这天就算完成
            if bool(getattr(status, "finished", False)):
                node["finished"] = True
            node["known"] = True
            node["unfinished_tasks"] = len(getattr(status, "unfinished_tasks", []) or [])

    days = _prune(days)
    data["days"] = days
    _atomic_write(data)
    save_report_snapshot(report, at=moment)
    logger.debug("已写入历史：%s（%d 个游戏）", date_key, len(day))
    return data


def _snapshot_alert_dict(alert: Any) -> Dict[str, Any]:
    """把一条活动提醒定格成可序列化 dict。

    ``warn`` 存的是 ``warn_line()`` 的当场输出：days_left 是按当前时刻
    现算的 property，不定格的话快照隔天回看倒计时会自己漂掉。
    """
    end = getattr(alert, "end", None)
    warn = ""
    try:
        warn = alert.warn_line() or ""
    except Exception:  # noqa: BLE001 - 单条提醒坏掉不拖累整份快照
        pass
    return {
        "game": getattr(alert, "game_id", "") or "",
        "game_name": getattr(alert, "game_name", "") or "",
        "title": getattr(alert, "title", "") or "",
        "end": end.strftime("%Y-%m-%d %H:%M:%S") if end else "",
        "warn": warn,
    }


def save_report_snapshot(report: Any, *, at: Optional[datetime] = None) -> None:
    """把上一轮巡检的完整判定快照原子写入 data/last_report.json。

    供 GET /api/push_preview 离线重建 DailyReport 再走 to_push_payload
    同一条组装路径——页面预览与手机实发共用一处文案逻辑。只写快照、
    不联网、不影响巡检本身：任何失败都只记 WARNING。
    """
    try:
        statuses = [
            status.to_dict()
            for status in (getattr(report, "statuses", None) or [])
            if hasattr(status, "to_dict")
        ]
        if not statuses:
            return   # 没有有效状态时不覆盖旧快照，预览继续显示上一轮真实结果
        moment = at or getattr(report, "generated_at", None) or datetime.now()
        payload = {
            "version": 1,
            "generated_at": moment.isoformat(timespec="seconds"),
            "statuses": statuses,
            "resource_alerts": [
                alert.to_dict()
                for alert in (getattr(report, "resource_alerts", None) or [])
                if hasattr(alert, "to_dict")
            ],
            "activity_alerts": [
                _snapshot_alert_dict(alert)
                for alert in (getattr(report, "activity_alerts", None) or [])
            ],
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        json.loads(text)  # 复检：序列化出来的必须是合法 JSON

        target = DATA_DIR / REPORT_SNAPSHOT_NAME
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".snapshot-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 - 快照是锦上添花，绝不能弄坏巡检落盘
        logger.warning("写入巡检快照失败（不影响巡检历史）：%s", exc)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def _game_meta(
    days: Dict[str, Any], extra: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """每个游戏的中文名与昵称（报表列头用）。

    来源与列的取舍：
        1. ``extra`` —— **当前配置里启用的游戏**：提供了它（非 None）时就
           **只列这些**——用户取消勾选的游戏从报表/热力图/KPI 全部消失
           （数据仍留在 days 里，重新勾选即原样恢复）；
        2. ``extra`` 为 None（调用方没有配置上下文）→ 维持旧行为：
           ``days`` 里出现过的游戏全部列出。
    名字兜底到 ``config.KNOWN_GAMES``，认不出来就用 game id 本身；
    启用游戏的昵称仍会从历史节点回填（有则用）。
    """
    meta: Dict[str, Dict[str, str]] = {}

    def put(game: str, name: str = "", nickname: str = "") -> None:
        item = meta.setdefault(
            game, {"game_name": KNOWN_GAMES.get(game, game), "nickname": ""}
        )
        if name:
            item["game_name"] = name
        if nickname:
            item["nickname"] = nickname

    extra_set = set(extra) if extra is not None else None

    for game in extra or []:
        if isinstance(game, str) and game:
            put(game)

    for date_key in sorted(days.keys()):
        day = days[date_key]
        if not isinstance(day, dict):   # 脏数据：跳过这一天，别让 .items() 炸掉
            continue
        for game, node in day.items():
            if not isinstance(node, dict):
                continue
            if extra_set is not None and game not in extra_set:
                continue   # 未勾选的游戏：不进报表列（数据保留，重新勾选即恢复）
            put(game, str(node.get("game_name") or ""), str(node.get("nickname") or ""))
    return meta


def _ordered_games(meta: Dict[str, Dict[str, str]]) -> List[str]:
    """列顺序：先按内置 GAME_ORDER，没登记过的新游戏排在后面（按 id 排序）。

    注意 GAME_ORDER 只决定"先画哪一列"，**不决定有没有这一列** ——
    会不会出现完全由上一步的 meta 决定，所以删掉某款游戏也不会漏列。
    """
    known = set(GAME_ORDER)
    ordered = [g for g in GAME_ORDER if g in meta]
    ordered += sorted(g for g in meta if g not in known)
    return ordered


def _streak(days: Dict[str, Any], game: str, today: datetime) -> int:
    """连续完成天数：从今天往回数，遇到"查过但没完成"就断。

    今天还没检查过（无记录）不算断 —— 今天还没结束，不能判人家失败；
    一路回溯到最早有记录那天为止。
    """
    if not days:
        return 0
    earliest = min(days.keys())
    streak = 0
    cursor = today
    for _ in range(KEEP_DAYS + 1):
        key = cursor.strftime("%Y-%m-%d")
        if key < earliest:
            break
        node = (days.get(key) or {}).get(game)
        if isinstance(node, dict):
            if node.get("finished"):
                streak += 1
            elif node.get("known"):
                break  # 查过、没做完 -> 断了
            # 查过但全是失败（unknown）：跳过，不算断
        cursor -= timedelta(days=1)
    return streak


def _month_stats(days: Dict[str, Any], game: str, month: str) -> Dict[str, int]:
    """本月：完成天数 / 未完成天数（没记录的日子不算未完成）。"""
    done = missed = 0
    for date_key in sorted(days.keys()):
        if not date_key.startswith(month):
            continue
        node = (days.get(date_key) or {}).get(game)
        if not isinstance(node, dict) or not node.get("known"):
            continue
        if node.get("finished"):
            done += 1
        else:
            missed += 1
    return {"done": done, "missed": missed}


def summary(
    days: int = DEFAULT_DAYS,
    *,
    today: Optional[datetime] = None,
    enabled_games: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """给 /api/history 用的汇总：最近 N 天明细 + 每个游戏的连胜 / 本月统计。

    Args:
        enabled_games: 当前配置里启用的游戏 id 列表。报表会**一定给它们留一列**，
            哪怕今天还没跑到（显示 –）；不传就退化成"写过什么显示什么"。

    任何内部异常都在最后兜底成 ``empty_payload()`` —— 报表页永远有东西可渲染，
    不会因为一份坏存档变成 HTTP 500（前端那种"红字弹窗"正是 500 引起的）。
    """
    days = max(1, min(int(days or DEFAULT_DAYS), 60))
    now = today or game_moment()   # 「今天」= 游戏日期（04:00 日界），KPI/热力图全靠它
    try:
        return _summary_unsafe(days, now, enabled_games)
    except Exception as exc:  # noqa: BLE001 - 统计永远不该让页面挂掉
        logger.exception("生成历史报表失败，返回空报表：%s", exc)
        return empty_payload(days, today=now)


def weekly_digest(days: int = DEFAULT_DAYS) -> Dict[str, Any]:
    """过去 N 个游戏日的逐游戏完成统计（daemon 周报与报表页「本周汇总」同口径）。

    一天的口径与 record() 一致：known = 当天查到过数据；done = 当天完成；
    missed = known - done（查过但没做完）。整天没查过的游戏不进结果，
    整周没有任何数据时 games 为空列表。任何异常兜底成空结构 ——
    周报不发可以，守护进程不能挂。
    """
    days = max(1, min(int(days or DEFAULT_DAYS), 60))
    try:
        data = load()
        store = data.get("days") or {}
        if not isinstance(store, dict):
            store = {}
        keys = sorted(
            k for k, v in store.items() if isinstance(k, str) and isinstance(v, dict)
        )[-days:]
        stats: Dict[str, Dict[str, int]] = {}
        for key in keys:
            for game, cell in store[key].items():
                if not isinstance(cell, dict) or not cell.get("known"):
                    continue
                st = stats.setdefault(game, {"known": 0, "done": 0})
                st["known"] += 1
                if cell.get("finished"):
                    st["done"] += 1
    except Exception:  # noqa: BLE001
        logger.exception("统计周报数据失败，返回空周报")
        return {"days_count": 0, "games": []}

    games: List[Dict[str, Any]] = []
    ordered = [g for g in GAME_ORDER if g in stats] + sorted(
        g for g in stats if g not in GAME_ORDER
    )
    for game in ordered:
        st = stats[game]
        games.append({
            "game": game,
            "name": KNOWN_GAMES.get(game, game),
            "done_days": st["done"],
            "known_days": st["known"],
            "missed_days": st["known"] - st["done"],
        })
    return {"days_count": len(keys), "games": games}


def _summary_unsafe(
    days: int,
    now: datetime,
    enabled_games: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """真正干活的实现；由 :func:`summary` 包一层异常兜底后再调用。"""
    today_key = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    data = load()
    store = data.get("days") or {}
    if not isinstance(store, dict):
        store = {}
    meta = _game_meta(store, enabled_games)
    games = _ordered_games(meta)

    # --- 最近 N 天明细（今天在最上面）---
    recent: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        cursor = now - timedelta(days=offset)
        key = cursor.strftime("%Y-%m-%d")
        day = store.get(key)
        if not isinstance(day, dict):   # 脏数据：这一天不是对象，当空处理
            day = {}
        cells: Dict[str, Any] = {}
        known_any = False
        all_done = True
        for game in games:
            node = day.get(game)
            if isinstance(node, dict) and node.get("known"):
                known_any = True
                finished = bool(node.get("finished"))
                cells[game] = {
                    "finished": finished,
                    "known": True,
                    "checks": int(node.get("checks") or 0),
                    "last": node.get("last") or "",
                    "unfinished_tasks": int(node.get("unfinished_tasks") or 0),
                }
                if not finished:
                    all_done = False
            else:
                cells[game] = {"finished": False, "known": False, "checks": 0, "last": ""}
        recent.append({
            "date": key,
            "label": f"{int(key[5:7])}/{int(key[8:10])}",
            "weekday": _WEEKDAYS[cursor.weekday()],
            "is_today": key == today_key,
            "games": cells,
            "all_done": bool(known_any and all_done),
        })

    # --- 每个游戏的连胜与本月统计 ---
    stats: List[Dict[str, Any]] = []
    for game in games:
        info = meta.get(game) or {}
        month_stat = _month_stats(store, game, month)
        stats.append({
            "game": game,
            "game_name": info.get("game_name") or game,
            "nickname": info.get("nickname") or "",
            "streak": _streak(store, game, now),
            "month_done": month_stat["done"],
            "month_missed": month_stat["missed"],
        })

    return {
        "ok": True,
        "path": str(HISTORY_PATH),
        "has_data": bool(store),
        "today": today_key,
        "month": month,
        "days_count": days,
        "days": recent,
        "games": stats,
        "tracked": games,
    }


def clear() -> None:
    """清空存档（界面上没暴露，留给手动维护 / 测试）。"""
    _atomic_write(_empty())
