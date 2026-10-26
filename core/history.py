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
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dailycheck.history")

#: 存档根目录：<项目>/data/history.json
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HISTORY_PATH = DATA_DIR / "history.json"

#: 最多保留多少天（按日期清理，一天一条聚合，180 天也就几 KB）
KEEP_DAYS = 180

#: 报表默认展示天数
DEFAULT_DAYS = 7

#: 列的固定顺序（没见过的游戏排在后面）
GAME_ORDER = ("genshin", "starrail", "zzz", "arknights", "endfield")

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def _empty() -> Dict[str, Any]:
    return {"version": 1, "days": {}}


def load() -> Dict[str, Any]:
    """读存档；文件不存在 / 损坏都返回空结构（不让统计功能拖垮主流程）。"""
    try:
        raw = HISTORY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        logger.warning("读取历史记录失败：%s", exc)
        return _empty()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("历史记录 JSON 损坏（%s），本次按空处理", exc)
        return _empty()

    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        return _empty()
    return data


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
    date_key = moment.strftime("%Y-%m-%d")
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

        ok = bool(getattr(status, "ok", False))
        if ok:
            # 完成是"粘"的：当天只要有一次查到全做完，这天就算完成
            if bool(getattr(status, "finished", False)):
                node["finished"] = True
            node["known"] = True
            node["unfinished_tasks"] = len(getattr(status, "unfinished_tasks", []) or [])

    days = _prune(days)
    data["days"] = days
    _atomic_write(data)
    logger.debug("已写入历史：%s（%d 个游戏）", date_key, len(day))
    return data


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def _game_meta(days: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """扫全表，拿到每个游戏最新的中文名与昵称（报表列头用）。"""
    meta: Dict[str, Dict[str, str]] = {}
    for date_key in sorted(days.keys()):
        for game, node in (days[date_key] or {}).items():
            if not isinstance(node, dict):
                continue
            item = meta.setdefault(game, {"game_name": game, "nickname": ""})
            if node.get("game_name"):
                item["game_name"] = node["game_name"]
            if node.get("nickname"):
                item["nickname"] = node["nickname"]
    return meta


def _ordered_games(meta: Dict[str, Dict[str, str]]) -> List[str]:
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


def summary(days: int = DEFAULT_DAYS, *, today: Optional[datetime] = None) -> Dict[str, Any]:
    """给 /api/history 用的汇总：最近 N 天明细 + 每个游戏的连胜 / 本月统计。"""
    days = max(1, min(int(days or DEFAULT_DAYS), 60))
    now = today or datetime.now()
    today_key = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    data = load()
    store = data.get("days") or {}
    meta = _game_meta(store)
    games = _ordered_games(meta)

    # --- 最近 N 天明细（今天在最上面）---
    recent: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        cursor = now - timedelta(days=offset)
        key = cursor.strftime("%Y-%m-%d")
        day = store.get(key) or {}
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
