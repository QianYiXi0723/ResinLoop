# 推送回压记账：按通道记录推送失败连击；连续 5 次失败 → 静默 6 小时（is_muted=True）
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("dailycheck.pushguard")
PUSH_FAIL_PATH = Path(__file__).resolve().parent.parent / "data" / "push_fail.json"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
def _load() -> Dict[str, Any]:
    try:
        raw = json.loads(PUSH_FAIL_PATH.read_text(encoding="utf-8"))
        if isinstance(raw.get("items"), dict):
            return {"version": 1, "items": raw["items"]}
        logger.warning("push_fail.json 结构异常（items 不是对象），按空账本处理")
    except FileNotFoundError:
        return {"version": 1, "items": {}}
    except (OSError, ValueError) as exc:
        logger.warning("push_fail.json 读取失败（%s），按空账本处理", exc)
    return {"version": 1, "items": {}}


def _save(data: Dict[str, Any]) -> None:
    PUSH_FAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PUSH_FAIL_PATH.parent), prefix=".pushg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, PUSH_FAIL_PATH)
    except OSError as exc:
        logger.warning("push_fail.json 写入失败（%s）", exc)


def _node(items: Dict[str, Any], channel: str) -> Dict[str, Any]:
    return items.setdefault(channel, {"streak": 0, "last_fail_at": "", "muted_until": None})


def is_muted(channel: str, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    data = _load()
    node = _node(data["items"], channel)
    muted_until = node.get("muted_until")
    if not muted_until:
        return False
    try:
        expired = now >= datetime.strptime(muted_until, _TIME_FMT)
    except ValueError:  # 时间串损坏：宁可继续静默，也绝不放大失败
        return True
    if expired:
        node["muted_until"] = None
        _save(data)
    return not expired


def record(channel: str, ok: bool, now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now()
    data = _load()
    node = _node(data["items"], channel)
    if ok:
        node["streak"] = 0
        node["muted_until"] = None
        _save(data)
        return None
    node["streak"] = int(node.get("streak", 0)) + 1
    node["last_fail_at"] = now.strftime(_TIME_FMT)
    if node["streak"] >= 5:
        node["muted_until"] = (now + timedelta(hours=6)).strftime(_TIME_FMT)
        _save(data)
        return "已静默 6 小时"
    _save(data)
    return None


def reset_all() -> None:
    """清空账本（仅供测试使用）。"""
    _save({"version": 1, "items": {}})
