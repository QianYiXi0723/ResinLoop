# -*- coding: utf-8 -*-
"""凭证健康记账（纯本地读写，不联网）。

记录每个账号每个游戏的查询成败连击，用于「凭证可能失效」的独立提醒：
连续 >=2 次 auth 类失败、且 24 小时内没有提醒过 → 返回一条 alert，
由 main.run_once 走独立推送（不参与日常推送的 should_push 门槛）。

key 形如 "<uid>:<game_id>"（与 logs/mihoyo_binding.json 语义对齐）；
取不到 uid 的账号由调用方用昵称 / 序号兜底，本模块只保存 key 原文，
game_id 取冒号后的第一段（无冒号则整个 key 视为 game_id）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("dailycheck.credhealth")

BASE_DIR = Path(__file__).resolve().parent.parent
CRED_HEALTH_PATH = BASE_DIR / "data" / "cred_health.json"

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_ALERT_COOLDOWN = timedelta(hours=24)


def _load() -> Dict[str, Any]:
    """读账本；不存在 / 损坏一律当空结构（带 warning），绝不抛出。"""
    try:
        raw = json.loads(CRED_HEALTH_PATH.read_text(encoding="utf-8"))
        items = raw.get("items")
        if isinstance(items, dict):
            return {"version": 1, "items": items}
        logger.warning("cred_health.json 结构异常（items 不是对象），按空账本处理")
        return {"version": 1, "items": {}}
    except FileNotFoundError:
        return {"version": 1, "items": {}}
    except (OSError, ValueError) as exc:
        logger.warning("cred_health.json 读取失败（%s），按空账本处理", exc)
        return {"version": 1, "items": {}}


def _save(data: Dict[str, Any]) -> None:
    """原子写盘：mkstemp + os.replace（与 core/notifier.py 同款写法）。"""
    CRED_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CRED_HEALTH_PATH.parent),
                               prefix=".credh-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, CRED_HEALTH_PATH)
    except OSError as exc:
        logger.warning("cred_health.json 写入失败（%s）", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TIME_FMT)


def record(key: str, ok: bool, kind: str = "",
           now: Optional[datetime] = None) -> Optional[dict]:
    """记一次查询结果。失败连击达到告警条件时返回 alert dict，否则返回 None。

    ok=True  → fail_streak=0、kind 清空、last_ok_at=now
    ok=False → fail_streak+1、last_fail_at/kind 更新；
               fail_streak >= 2 且 kind == "auth" 且（从未提醒过 或 距上次 >= 24h）
               时写 alerted_at 并返回 alert。
    """
    now = now or datetime.now()
    data = _load()
    items: Dict[str, Any] = data.setdefault("items", {})
    node = items.setdefault(key, {"fail_streak": 0, "last_ok_at": "",
                                  "last_fail_at": "", "alerted_at": "", "kind": ""})
    if ok:
        node["fail_streak"] = 0
        node["kind"] = ""
        node["last_ok_at"] = now.strftime(_TIME_FMT)
        _save(data)
        return None

    node["fail_streak"] = int(node.get("fail_streak", 0)) + 1
    node["last_fail_at"] = now.strftime(_TIME_FMT)
    node["kind"] = kind
    alert = None
    alerted_at = node.get("alerted_at") or ""
    try:
        cooled = not alerted_at or (now - _parse(alerted_at)) >= _ALERT_COOLDOWN
    except ValueError:  # 时间串损坏：按从未提醒过处理，宁可多提醒一次也不静默
        cooled = True
    if node["fail_streak"] >= 2 and kind == "auth" and cooled:
        node["alerted_at"] = now.strftime(_TIME_FMT)
        alert = {"key": key,
                 "game_id": key.split(":", 1)[1] if ":" in key else key,
                 "streak": node["fail_streak"], "kind": kind}
    _save(data)
    return alert


def reset_all() -> None:
    """清空账本（仅供测试使用）。"""
    _save({"version": 1, "items": {}})
