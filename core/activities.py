"""活动日历：拉取「即将结束」的游戏内活动，并缓存到 data/activities.json。

设计要点
--------
* **只认官方公开接口**：原神用米哈游公告接口 ``getAnnList`` 的「活动公告」分组，
  里面每条都带真实的 ``start_time`` / ``end_time``。
* **每天最多拉一次**：缓存 20 小时有效，定时检查每轮调用 ``ensure_fresh()``
  也不会真的发请求；界面另有「刷新」按钮可强制拉取。
* **拿不到就明说**：星铁公告接口没有活动分组、绝区零公告接口异常、森空岛没有
  公开活动日历 —— 这些游戏在界面上标注「暂不支持」，绝不编造活动数据。
* **绝不拖垮主流程**：网络失败只记日志，沿用旧缓存；任何异常都不外抛。
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("dailycheck.activities")

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE_DIR / "data" / "activities.json"

#: 缓存有效期：20 小时（每天最多真正请求一次）
CACHE_TTL_HOURS = 20
#: 推送提醒窗口：活动结束前 72 小时开始提醒
ALERT_WINDOW_HOURS = 72
#: 单次请求超时（秒）—— 拉不到就算了，不能卡住定时检查
REQUEST_TIMEOUT = 8
#: 缓存文件里保留多少天的「已提醒」记录
NOTIFIED_KEEP_DAYS = 7

#: 已确认没有可用公开活动接口的游戏：界面上明确标注「暂不支持」。
UNSUPPORTED: Tuple[Dict[str, str], ...] = (
    {
        "game": "starrail",
        "game_name": "崩坏：星穹铁道",
        "reason": "官方公告接口只有版本/说明类公告，没有活动分组与截止时间",
    },
    {
        "game": "zzz",
        "game_name": "绝区零",
        "reason": "公告接口当前返回异常，暂未找到可用的活动列表",
    },
    {
        "game": "arknights",
        "game_name": "明日方舟",
        "reason": "森空岛未公开活动日历接口",
    },
    {
        "game": "endfield",
        "game_name": "终末地",
        "reason": "森空岛未公开活动日历接口",
    },
)

GENSHIN_ANN_URL = "https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/getAnnList"
GENSHIN_ANN_PARAMS: Dict[str, Any] = {
    "game": "hk4e",
    "game_biz": "hk4e_cn",
    "lang": "zh-cn",
    "bundle_id": "hk4e_cn",
    "platform": "pc",
    "region": "cn_gf01",
    "level": 60,
    "uid": "100000000",
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Activity:
    """一个正在进行中的活动。"""

    game_id: str
    game_name: str
    title: str
    start: datetime
    end: datetime
    ann_id: int = 0

    @property
    def hours_left(self) -> float:
        return max((self.end - datetime.now()).total_seconds() / 3600.0, 0.0)

    @property
    def days_left(self) -> int:
        """向上取整的天数：47 小时 -> 2 天，23 小时 -> 1 天。"""
        return max(int(math.ceil(self.hours_left / 24.0)), 1 if self.hours_left > 0 else 0)

    def to_dict(self) -> Dict[str, Any]:
        hours = self.hours_left
        return {
            "game": self.game_id,
            "game_name": self.game_name,
            "title": self.title,
            "start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": self.end.strftime("%Y-%m-%d %H:%M:%S"),
            "ann_id": self.ann_id,
            "hours_left": round(hours, 1),
            "days_left": self.days_left,
            "ending_soon": 0 < hours <= ALERT_WINDOW_HOURS,
        }


@dataclass
class ActivityAlert:
    """活动结束提醒（推送正文用）。"""

    game_id: str
    game_name: str
    title: str
    end: datetime
    ann_id: int = 0

    @property
    def days_left(self) -> int:
        hours = max((self.end - datetime.now()).total_seconds() / 3600.0, 0.0)
        if hours <= 0:
            return 0
        return max(int(math.ceil(hours / 24.0)), 1)

    def warn_line(self) -> str:
        """📅 活动提醒：原神「祝圣移涌」，还剩 2 天结束，请抓紧时间！

        不少公告标题本身就带「」，这里只补外层缺失的引号，避免出现「「…」」。
        """
        days = self.days_left
        shown = self.title if self.title.startswith("「") else f"「{self.title}」"
        left = f"还剩 {days} 天" if days >= 1 else "今天就要"
        return f"📅 活动提醒：{self.game_name}{shown}，{left}结束，请抓紧时间！"


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #


def _empty_cache() -> Dict[str, Any]:
    return {"version": 1, "fetched_at": "", "last_attempt": "", "items": [], "notified": {}}


def load_cache() -> Dict[str, Any]:
    """读缓存；文件不存在 / 损坏 / 结构不对都返回空结构，绝不抛异常。"""
    try:
        raw = CACHE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_cache()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("读取活动缓存失败（按空处理）：%s", exc)
        return _empty_cache()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("活动缓存 JSON 损坏（按空处理）：%s", exc)
        return _empty_cache()
    if not isinstance(data, dict):
        return _empty_cache()
    if not isinstance(data.get("items"), list):
        data["items"] = []
    if not isinstance(data.get("notified"), dict):
        data["notified"] = {}
    return data


def save_cache(data: Dict[str, Any]) -> None:
    """原子写缓存：唯一临时文件 + os.replace，避免并发时互相删掉对方的 tmp。"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".activities-", suffix=".tmp", dir=str(CACHE_PATH.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CACHE_PATH)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # 清理失败不影响已经写完的缓存
                pass


def _parse_time(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _cache_age_hours(fetched_at: str) -> Optional[float]:
    moment = _parse_time(fetched_at)
    if moment is None:
        return None
    return (datetime.now() - moment).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# 拉取
# --------------------------------------------------------------------------- #


def fetch_genshin_activities() -> List[Dict[str, Any]]:
    """拉原神「活动公告」分组，返回带真实起止时间的活动列表。"""
    import requests  # 惰性导入：没装依赖时也不影响其它功能

    session = requests.Session()
    session.trust_env = False  # 本机系统代理会拦 127.0.0.1 / 部分外网，直连更稳
    response = session.get(GENSHIN_ANN_URL, params=GENSHIN_ANN_PARAMS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retcode") not in (0, None):
        raise RuntimeError(f"公告接口返回 retcode={payload.get('retcode')}：{payload.get('message')}")

    data = payload.get("data") or {}
    groups = data.get("list") or []
    if not isinstance(groups, list):
        raise RuntimeError("公告接口返回结构异常（list 不是数组）")

    now = datetime.now()
    result: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        # 原神的活动在 type_id == 1「活动公告」；用 label 兜底，避免 id 变动
        if group.get("type_id") != 1 and group.get("type_label") != "活动公告":
            continue
        for item in group.get("list") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            start = _parse_time(item.get("start_time"))
            end = _parse_time(item.get("end_time"))
            if not title or not start or not end:
                continue
            if end <= now or start > now:
                continue  # 只留"正在进行中"的
            if title in seen:
                continue
            seen.add(title)
            result.append({
                "game": "genshin",
                "game_name": "原神",
                "title": title,
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "ann_id": int(item.get("ann_id") or 0),
            })
    result.sort(key=lambda item: item["end"])
    return result


#: 目前只有原神有可用的官方活动接口；新增游戏时在这里挂一个 fetch 函数即可。
FETCHERS = {"genshin": fetch_genshin_activities}


def refresh(force: bool = False) -> Dict[str, Any]:
    """刷新缓存（默认 20 小时内不重复请求）。失败保留旧数据，只记 last_error。"""
    cache = load_cache()
    age = _cache_age_hours(cache.get("fetched_at", ""))
    if not force and age is not None and age < CACHE_TTL_HOURS:
        return summary()

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    collected: List[Dict[str, Any]] = []
    errors: List[str] = []
    for game_id, fetcher in FETCHERS.items():
        try:
            collected.extend(fetcher())
        except Exception as exc:  # noqa: BLE001 - 拉不到活动不能影响主流程
            logger.warning("拉取 %s 活动失败：%s", game_id, exc)
            errors.append(f"{game_id}: {exc}")

    cache["last_attempt"] = now_text
    if errors and not collected:
        cache["last_error"] = "；".join(errors)
        save_cache(cache)
        logger.warning("活动缓存未更新（沿用旧数据）：%s", cache["last_error"])
        return summary()

    cache["items"] = collected
    cache["fetched_at"] = now_text
    cache["source"] = {"genshin": "miyoushe-announcement"}
    cache.pop("last_error", None)
    cache["notified"] = _prune_notified(cache.get("notified") or {})
    save_cache(cache)
    logger.info("活动缓存已更新：%d 个进行中的活动", len(collected))
    return summary()


def ensure_fresh() -> Dict[str, Any]:
    """定时检查每轮调用：缓存过期才真的去拉，异常一律吞掉。"""
    try:
        return refresh(force=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("检查活动缓存失败（不影响本轮提醒）：%s", exc)
        return summary()


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def ongoing() -> List[Activity]:
    """当前仍在进行中的活动（只读缓存，不发请求）。"""
    cache = load_cache()
    now = datetime.now()
    result: List[Activity] = []
    for item in cache.get("items") or []:
        if not isinstance(item, dict):
            continue
        start = _parse_time(item.get("start"))
        end = _parse_time(item.get("end"))
        if not start or not end or end <= now or start > now:
            continue
        result.append(Activity(
            game_id=str(item.get("game") or ""),
            game_name=str(item.get("game_name") or ""),
            title=str(item.get("title") or ""),
            start=start,
            end=end,
            ann_id=int(item.get("ann_id") or 0),
        ))
    result.sort(key=lambda act: act.end)
    return result


def pending_alerts(
    hours: int = ALERT_WINDOW_HOURS,
    *,
    today: Optional[datetime] = None,
) -> List[ActivityAlert]:
    """剩余 <= hours 的活动，且今天还没提醒过。"""
    day_key = (today or datetime.now()).strftime("%Y-%m-%d")
    cache = load_cache()
    raw_notified = cache.get("notified", {}).get(day_key, [])
    notified = set(raw_notified) if isinstance(raw_notified, list) else set()
    alerts: List[ActivityAlert] = []
    for act in ongoing():
        left = act.hours_left
        if not (0 < left <= hours):
            continue
        if act.ann_id and act.ann_id in notified:
            continue
        alerts.append(ActivityAlert(
            game_id=act.game_id, game_name=act.game_name, title=act.title,
            end=act.end, ann_id=act.ann_id,
        ))
    return alerts


def mark_notified(alerts: Iterable[ActivityAlert], *, today: Optional[datetime] = None) -> None:
    """把今天已经推送过的活动记下来，避免同一天每轮检查都催一遍。"""
    day_key = (today or datetime.now()).strftime("%Y-%m-%d")
    ids = [alert.ann_id for alert in alerts if alert.ann_id]
    if not ids:
        return
    cache = load_cache()
    notified = cache.get("notified") or {}
    if not isinstance(notified, dict):
        notified = {}
    raw_current = notified.get(day_key)
    current = list(raw_current) if isinstance(raw_current, list) else []
    for ann_id in ids:
        if ann_id not in current:
            current.append(ann_id)
    notified[day_key] = current
    cache["notified"] = _prune_notified(notified)
    try:
        save_cache(cache)
    except OSError as exc:
        logger.warning("记录活动提醒失败（下次可能重复提醒）：%s", exc)


def _prune_notified(notified: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = datetime.now() - timedelta(days=NOTIFIED_KEEP_DAYS)
    kept: Dict[str, Any] = {}
    for key, value in notified.items():
        moment = _parse_time(str(key) + " 00:00:00")
        if moment is None or moment >= cutoff:
            kept[key] = value
    return kept


def summary() -> Dict[str, Any]:
    """给界面/接口用的汇总：进行中的活动 + 暂不支持的游戏 + 缓存状态。"""
    cache = load_cache()
    fetched_at = str(cache.get("fetched_at") or "")
    age = _cache_age_hours(fetched_at)
    activities = [act.to_dict() for act in ongoing()]
    return {
        "ok": True,
        "has_data": bool(fetched_at),
        "fetched_at": fetched_at,
        "stale": (age is None) or (age >= CACHE_TTL_HOURS),
        "age_hours": round(age, 1) if age is not None else None,
        "last_error": str(cache.get("last_error") or ""),
        "window_hours": ALERT_WINDOW_HOURS,
        "items": activities,
        "unsupported": [dict(item) for item in UNSUPPORTED],
    }
