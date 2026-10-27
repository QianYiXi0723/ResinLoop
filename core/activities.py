"""活动日历：拉取各游戏官方的「活动日历」，缓存到 data/activities.json。

数据来源（都是已登录账号的官方接口，不是公告）
------------------------------------------------
* **原神**：``POST api-takumi-record…/genshin/api/act_calendar``
  → ``act_list``（活动）+ ``fixed_act_list``（渊月螺旋等固定挑战）
* **星铁**：``GET api-takumi-record…/hkrpg/api/get_act_calender``
  （官方拼错的 calender）→ ``act_list`` + ``challenge_list``
* **绝区零**：``GET api-takumi-record…/zzz/activity_calendar``
  （参数是 ``uid`` / ``region``，不是 role_id/server）→ ``activity_list``
* **明日方舟**：鹰角官网 ``api/news`` 的 ``tab=1``，结束时间解析自活动正文

设计要点
--------
* **只认「日历」**：不再抓游戏公告。日历里每一条都有名字、起止时间、
  进度和状态，和米游社 App 里「活动日历」看到的是同一份数据。
* **每天最多拉一次**：缓存 20 小时有效；界面另有「刷新」可强制拉取。
* **不想要的直接删**：删掉的活动记在 ``data/ignored_activities.json``，
  拉取之后立刻过滤，**永远不会再进列表、也不会再触发推送**。
* **绝不拖垮主流程**：单个游戏失败只丢那一款，网络异常一律不外抛。
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("dailycheck.activities")

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE_DIR / "data" / "activities.json"
IGNORED_PATH = BASE_DIR / "data" / "ignored_activities.json"

#: 缓存结构版本。版本不一致时下一轮强制重拉（老缓存是公告，结构对不上）。
CACHE_VERSION = 3

#: 缓存有效期：20 小时（每天最多真正请求一次）
CACHE_TTL_HOURS = 20
#: 推送提醒窗口：活动结束前 72 小时开始提醒
ALERT_WINDOW_HOURS = 72
#: 单次请求超时（秒）—— 拉不到就算了，不能卡住定时检查
REQUEST_TIMEOUT = 8
#: 缓存文件里保留多少天的「已提醒」记录
NOTIFIED_KEEP_DAYS = 7

#: 拿不到结构化日历的游戏（其余四款都已接入）
UNSUPPORTED: Tuple[Dict[str, str], ...] = (
    {
        "game": "endfield",
        "game_name": "终末地",
        "reason": "官方只有公告且不含截止日期，没有可用的活动日历接口",
    },
)

GENSHIN_CAL_URL = (
    "https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/act_calendar"
)
STARRAIL_CAL_URL = (
    "https://api-takumi-record.mihoyo.com/game_record/app/hkrpg/api/get_act_calender"
)
ZZZ_CAL_URL = (
    "https://api-takumi-record.mihoyo.com/event/game_record_zzz/api/zzz/activity_calendar"
)

AK_NEWS_URL = "https://ak.hypergryph.com/api/news"
AK_NEWS_PAGE = "https://ak.hypergryph.com/news/%s"
#: 最多翻几条活动详情（每条一次请求，只取最近的一批）
AK_DETAIL_LIMIT = 6

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: 方舟正文里的活动时间，兼容：
#:   10月24日20:00~11月24日03:59 / 11月01日 12:00 - 11月15日 03:59
_DATE_WINDOW_RE = re.compile(
    r"活动时间[：:]\s*"
    r"(?:(\d{4})[年/])?(\d{1,2})[月/](\d{1,2})日?\s*(\d{1,2}:\d{2})?\s*"
    r"[~～\-—至到]+"
    r"\s*(?:(\d{4})[年/])?(\d{1,2})[月/](\d{1,2})日?\s*(\d{1,2}:\d{2})?"
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


def _clean_text(text: Any) -> str:
    """去掉 HTML 标签 / 反转义 / 压平空白。"""
    raw = _HTML_TAG_RE.sub(" ", str(text or ""))
    return _WS_RE.sub(" ", html.unescape(raw)).strip()


@dataclass
class Activity:
    """活动日历里的一条。"""

    game_id: str
    game_name: str
    title: str
    start: datetime
    end: datetime
    kind: str = "act"          # act=活动 challenge=固定挑战（深渊/忘却之庭）
    status: str = ""           # 进行中 / 未开启 / 已完成（没给就按时间算）
    progress: str = ""         # 36/36 这种进度，没有就空
    icon: str = ""             # 活动图标（界面用）
    remote_id: str = ""        # 官方活动 id，用于生成稳定 uid

    @property
    def uid(self) -> str:
        key = self.remote_id or self.title
        return f"{self.game_id}:{self.kind}:{key}"

    @property
    def hours_left(self) -> float:
        return max((self.end - datetime.now()).total_seconds() / 3600.0, 0.0)

    @property
    def days_left(self) -> int:
        """向上取整的天数：47 小时 -> 2 天，23 小时 -> 1 天。"""
        return max(int(math.ceil(self.hours_left / 24.0)), 1 if self.hours_left > 0 else 0)

    @property
    def not_started(self) -> bool:
        return datetime.now() < self.start

    def countdown_text(self) -> str:
        """界面上的倒计时文案：和 App 里「5天9小时」一个意思。"""
        now = datetime.now()
        if self.not_started:
            left = self.start - now
            return f"{_human_gap(left)}后开启"
        if self.end <= now:
            return "已结束"
        return f"剩 {_human_gap(self.end - now)}"

    def to_dict(self) -> Dict[str, Any]:
        hours = self.hours_left
        return {
            "id": self.uid,
            "game": self.game_id,
            "game_name": self.game_name,
            "title": self.title,
            "kind": self.kind,
            "start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": self.end.strftime("%Y-%m-%d %H:%M:%S"),
            "hours_left": round(hours, 1),
            "days_left": self.days_left,
            "ending_soon": 0 < hours <= ALERT_WINDOW_HOURS and not self.not_started,
            "not_started": self.not_started,
            "countdown": self.countdown_text(),
            "status": self.status or ("未开启" if self.not_started else "进行中"),
            "progress": self.progress,
            "icon": self.icon,
        }


def _human_gap(delta: timedelta) -> str:
    """1 天 3 小时 / 5 小时 12 分 —— 粗到小时就够，别做秒级抖动。"""
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    return f"{minutes} 分"


@dataclass
class ActivityAlert:
    """活动结束提醒（推送正文用）。"""

    game_id: str
    game_name: str
    title: str
    end: datetime
    uid: str = ""

    @property
    def days_left(self) -> int:
        hours = max((self.end - datetime.now()).total_seconds() / 3600.0, 0.0)
        if hours <= 0:
            return 0
        return max(int(math.ceil(hours / 24.0)), 1)

    def warn_line(self) -> str:
        """📅 活动提醒：原神「祝圣移涌」，还剩 2 天结束，请抓紧时间！"""
        days = self.days_left
        needs_quote = "「" not in self.title and "」" not in self.title
        shown = f"「{self.title}」" if needs_quote else self.title
        left = f"还剩 {days} 天" if days >= 1 else "今天就要"
        return f"📅 活动提醒：{self.game_name}{shown}，{left}结束，请抓紧时间！"


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #


def _empty_cache() -> Dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "fetched_at": "",
        "last_attempt": "",
        "items": [],
        "notified": {},
    }


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("读取 %s 失败（按空处理）：%s", path.name, exc)
        return default
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("%s JSON 损坏（按空处理）：%s", path.name, exc)
        return default
    return data if isinstance(data, dict) else default


def load_cache() -> Dict[str, Any]:
    """读缓存；文件不存在 / 损坏 / 结构不对都返回空结构，绝不抛异常。"""
    data = _read_json(CACHE_PATH, _empty_cache())
    if not isinstance(data.get("items"), list):
        data["items"] = []
    if not isinstance(data.get("notified"), dict):
        data["notified"] = {}
    return data


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    """原子写：唯一临时文件 + os.replace，并发时不会互相删掉对方的 tmp。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.stem + "-", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # 清理失败不影响已经写完的文件
                pass


def save_cache(data: Dict[str, Any]) -> None:
    _atomic_write(CACHE_PATH, data)


# --------------------------------------------------------------------------- #
# 黑名单：删掉的活动记在这里，永远不再出现
# --------------------------------------------------------------------------- #


def load_ignored() -> Dict[str, Any]:
    data = _read_json(IGNORED_PATH, {"version": 1, "ids": [], "updated_at": ""})
    ids = data.get("ids")
    if not isinstance(ids, list):
        data["ids"] = []
    else:
        data["ids"] = [str(item) for item in ids if isinstance(item, str)]
    return data


def _ignored_ids() -> set:
    return set(load_ignored().get("ids") or [])


_IGNORED_WRITE_LOCK = threading.Lock()


def ignore_activity(uid: str) -> Dict[str, Any]:
    """把一条活动加入黑名单：立刻从列表消失，且不会再触发推送。"""
    target = str(uid or "").strip()
    if not target:
        raise ValueError("缺少活动 ID")
    with _IGNORED_WRITE_LOCK:
        data = load_ignored()
        ids = data.get("ids") or []
        if target not in ids:
            ids.append(target)
        data["ids"] = ids
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(IGNORED_PATH, data)
        logger.info("活动已隐藏（加入黑名单）：%s", target)
    return summary()


def restore_ignored() -> Dict[str, Any]:
    """清空黑名单（界面上的「全部恢复」用）。"""
    with _IGNORED_WRITE_LOCK:
        _atomic_write(
            IGNORED_PATH,
            {"version": 1, "ids": [], "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        )
    return summary()


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #


def _parse_time(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    text = text.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _from_ts(value: Any) -> Optional[datetime]:
    try:
        stamp = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp)
    except (OSError, ValueError, OverflowError):
        return None


def _cache_age_hours(fetched_at: str) -> Optional[float]:
    moment = _parse_time(fetched_at)
    if moment is None:
        return None
    return (datetime.now() - moment).total_seconds() / 3600.0


def _window_from_text(text: str, base: datetime) -> Optional[Tuple[datetime, datetime]]:
    """从公告正文里抠出「活动时间：A ~ B」（只给方舟用）。"""
    match = _DATE_WINDOW_RE.search(text)
    if not match:
        return None
    sy, sm, sd, st, ey, em, ed, et = match.groups()
    try:
        start_month, start_day = int(sm), int(sd)
        end_month, end_day = int(em), int(ed)
        start_year = int(sy) if sy else base.year
        if not sy and start_month < base.month - 6:
            start_year += 1
        end_year = int(ey) if ey else start_year
        if not ey and (end_month, end_day) < (start_month, start_day):
            end_year += 1
        sh, smin = (int(st[:2]), int(st[3:])) if st else (0, 0)
        eh, emin = (int(et[:2]), int(et[3:])) if et else (23, 59)
        start = datetime(start_year, start_month, start_day, sh, smin)
        end = datetime(end_year, end_month, end_day, eh, emin)
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


# --------------------------------------------------------------------------- #
# 米游社活动日历（原神 / 星铁 / 绝区零）
# --------------------------------------------------------------------------- #


def _miyoushe_credentials(game_id: str):
    """取某个游戏已绑定账号的 (ltuid, ltoken, uid, server, cookie)。"""
    from core import mihoyo_api  # 惰性导入，避免没配账号时拖慢启动
    from core.config import load_config

    config = load_config(BASE_DIR / "config.json", auto_create=False)
    for account in config.accounts:
        if account.game == game_id and account.enabled:
            return mihoyo_api.extract_credentials(account)
    raise RuntimeError(f"{game_id} 还没有绑定米游社账号，无法读取活动日历")


def _miyoushe_session():
    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _miyoushe_get(url: str, params: Dict[str, Any], creds) -> Dict[str, Any]:
    from core import mihoyo_api

    ltuid, ltoken, uid, server, cookie = creds
    headers = mihoyo_api.build_headers(
        ltuid, ltoken, query=params, cookie=cookie, with_device=False
    )
    session = _miyoushe_session()
    response = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    retcode = payload.get("retcode")
    if retcode not in (0, None):
        raise RuntimeError(
            f"活动日历接口返回 retcode={retcode}：{payload.get('message')}"
        )
    return payload.get("data") or {}


def _miyoushe_post(url: str, body: Dict[str, Any], creds) -> Dict[str, Any]:
    from core import mihoyo_api

    ltuid, ltoken, uid, server, cookie = creds
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    headers = mihoyo_api.build_headers(ltuid, ltoken, query={}, cookie=cookie, with_device=False)
    headers["DS"] = mihoyo_api.build_ds(query={}, body=text)
    headers["Content-Type"] = "application/json"
    session = _miyoushe_session()
    response = session.post(url, data=text.encode("utf-8"), headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    retcode = payload.get("retcode")
    if retcode not in (0, None):
        raise RuntimeError(
            f"活动日历接口返回 retcode={retcode}：{payload.get('message')}"
        )
    return payload.get("data") or {}


def _pick_time(info: Any, prefix: str = "") -> Tuple[Optional[datetime], Optional[datetime]]:
    """从 time_info / 顶层字段里取 (start, end)，时间戳优先。"""
    if not isinstance(info, dict):
        return None, None
    start = _from_ts(info.get(f"{prefix}start_ts")) or _parse_time(info.get(f"{prefix}start_time"))
    end = _from_ts(info.get(f"{prefix}end_ts")) or _parse_time(info.get(f"{prefix}end_time"))
    return start, end


def _reward_icon(rewards: Any) -> str:
    if isinstance(rewards, list):
        for item in rewards:
            if isinstance(item, dict) and item.get("icon"):
                return str(item["icon"])
    return ""


def fetch_genshin_activities() -> List[Dict[str, Any]]:
    """原神 act_calendar：活动 + 固定挑战（渊月螺旋）。"""
    creds = _miyoushe_credentials("genshin")
    _ltuid, _ltoken, uid, server, _cookie = creds
    data = _miyoushe_post(GENSHIN_CAL_URL, {"role_id": uid, "server": server}, creds)

    now = datetime.now()
    result: List[Dict[str, Any]] = []
    for raw in data.get("act_list") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _pick_time({
            "start_ts": raw.get("start_timestamp"), "end_ts": raw.get("end_timestamp"),
            "start_time": (raw.get("start_time") or {}).get("year") and _fmt_cn(raw.get("start_time")),
            "end_time": (raw.get("end_time") or {}).get("year") and _fmt_cn(raw.get("end_time")),
        })
        title = str(raw.get("name") or "").strip()
        if not title or not start or not end or end <= now:
            continue
        finished = bool(raw.get("is_finished"))
        result.append({
            "game": "genshin", "game_name": "原神", "kind": "act", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已完成" if finished else "",
            "progress": "",
            "icon": _reward_icon(raw.get("reward_list")),
            "remote_id": str(raw.get("id") or ""),
        })

    for raw in data.get("fixed_act_list") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _pick_time({
            "start_ts": raw.get("start_timestamp"), "end_ts": raw.get("end_timestamp"),
        })
        title = str(raw.get("name") or "").strip()
        if not title or not start or not end or end <= now:
            continue
        progress = ""
        tower = raw.get("tower_detail") or {}
        if isinstance(tower, dict):
            got, total = tower.get("star_num"), tower.get("total_star_num")
            if total:
                progress = f"{got or 0}/{total}"
        result.append({
            "game": "genshin", "game_name": "原神", "kind": "challenge", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "", "progress": progress,
            "icon": _reward_icon(raw.get("reward_list")),
            "remote_id": str(raw.get("id") or ""),
        })
    result.sort(key=lambda item: item["end"])
    return result


def _fmt_cn(node: Any) -> str:
    """{"year":2026,"month":11,"day":2,"hour":3,"minute":59,"second":0} -> 字符串。"""
    try:
        return "%04d-%02d-%02d %02d:%02d:%02d" % (
            int(node["year"]), int(node["month"]), int(node["day"]),
            int(node.get("hour", 0)), int(node.get("minute", 0)), int(node.get("second", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return ""


def fetch_starrail_activities() -> List[Dict[str, Any]]:
    """星铁 get_act_calender：活动 + 挑战（忘却之庭等）。"""
    creds = _miyoushe_credentials("starrail")
    _ltuid, _ltoken, uid, server, _cookie = creds
    data = _miyoushe_get(STARRAIL_CAL_URL, {"role_id": uid, "server": server}, creds)

    now = datetime.now()
    result: List[Dict[str, Any]] = []
    for raw in data.get("act_list") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _pick_time(raw.get("time_info"))
        title = str(raw.get("name") or "").strip()
        if not title or not start or not end or end <= now:
            continue
        status_map = {
            "OtherActStatusUnFinish": "",
            "OtherActStatusFinish": "已完成",
            "DoubleRewardActStatusUnopened": "未开启",
            "OtherActStatusUnopened": "未开启",
        }
        progress = ""
        total, current = raw.get("total_progress"), raw.get("current_progress")
        if isinstance(total, int) and total > 0:
            progress = f"{current or 0}/{total}"
        result.append({
            "game": "starrail", "game_name": "崩坏：星穹铁道", "kind": "act", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": status_map.get(str(raw.get("act_status") or ""), ""),
            "progress": progress,
            "icon": _reward_icon(raw.get("reward_list")),
            "remote_id": str(raw.get("id") or ""),
        })

    for raw in data.get("challenge_list") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _pick_time(raw.get("time_info"))
        title = str(raw.get("name_mi18n") or raw.get("name") or "").strip()
        if not title or not start or not end or end <= now:
            continue
        progress = ""
        total, current = raw.get("total_progress"), raw.get("current_progress")
        if isinstance(total, int) and total > 0:
            progress = f"{current or 0}/{total}"
        result.append({
            "game": "starrail", "game_name": "崩坏：星穹铁道", "kind": "challenge", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "", "progress": progress,
            "icon": "",
            "remote_id": str(raw.get("group_id") or raw.get("challenge_type") or title),
        })
    result.sort(key=lambda item: item["end"])
    return result


def fetch_zzz_activities() -> List[Dict[str, Any]]:
    """绝区零 activity_calendar：参数是 uid / region，不是 role_id / server。"""
    creds = _miyoushe_credentials("zzz")
    _ltuid, _ltoken, uid, server, _cookie = creds
    data = _miyoushe_get(ZZZ_CAL_URL, {"uid": uid, "region": server}, creds)

    now = datetime.now()
    result: List[Dict[str, Any]] = []
    for raw in data.get("activity_list") or []:
        if not isinstance(raw, dict):
            continue
        start, end = _from_ts(raw.get("start_ts")), _from_ts(raw.get("end_ts"))
        title = str(raw.get("name") or "").strip()
        if not title or not start or not end or end <= now:
            continue
        progress = ""
        total, got = raw.get("monochrome_cnt"), raw.get("monochrome_got_cnt")
        if isinstance(total, int) and total > 0:
            progress = f"{got or 0}/{total}"
        state = str(raw.get("state") or "")
        status = "已完成" if state == "STATE_FINISHED" else ""
        result.append({
            "game": "zzz", "game_name": "绝区零", "kind": "act", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": status, "progress": progress,
            "icon": "",
            "remote_id": str(raw.get("activity_id") or ""),
        })
    result.sort(key=lambda item: item["end"])
    return result


# --------------------------------------------------------------------------- #
# 明日方舟（鹰角官网）
# --------------------------------------------------------------------------- #


def _new_session():
    """建一个直连会话：本机系统代理会拦部分外网，trust_env=False 更稳。"""
    import requests

    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": BROWSER_UA})
    return session


def _fetch_detail_window(session, url: str, base: datetime) -> Optional[Tuple[datetime, datetime]]:
    """抓公告正文并解析活动时间；抓不到 / 没写清楚就返回 None（不猜）。"""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - 单条详情失败不影响其它活动
        logger.debug("读取活动正文失败 %s：%s", url, exc)
        return None
    return _window_from_text(_clean_text(response.text), base)


def fetch_arknights_activities() -> List[Dict[str, Any]]:
    """明日方舟：鹰角官网 news 接口（tab=1 是活动），起止时间解析自正文。"""
    session = _new_session()
    response = session.get(
        AK_NEWS_URL,
        params={"lang": "zh-cn", "page": 1, "pageSize": 20},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"官网新闻接口返回 code={payload.get('code')}")

    now = datetime.now()
    result: List[Dict[str, Any]] = []
    checked = 0
    for item in (payload.get("data") or {}).get("list") or []:
        if not isinstance(item, dict) or str(item.get("tab")) != "1":
            continue  # tab: 0=公告 1=活动 2=制作组通讯
        title = _clean_text(item.get("title"))
        cid = str(item.get("cid") or "").strip()
        if not title or not cid:
            continue
        if checked >= AK_DETAIL_LIMIT:
            break
        checked += 1
        published = now
        stamp = item.get("displayTime")
        if isinstance(stamp, (int, float)) and stamp > 0:
            try:
                published = datetime.fromtimestamp(int(stamp))
            except (OSError, ValueError, OverflowError):
                published = now
        window = _fetch_detail_window(session, AK_NEWS_PAGE % cid, published)
        if not window:
            logger.debug("方舟活动正文里没有明确的起止时间，跳过：%s", title)
            continue
        start, end = window
        if end <= now:
            continue
        result.append({
            "game": "arknights", "game_name": "明日方舟", "kind": "act", "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "", "progress": "", "icon": "",
            "remote_id": cid,
        })
    result.sort(key=lambda item: item["end"])
    return result


#: 每个抓取器对应的来源说明，写进缓存的 source 字段方便排查
SOURCES: Dict[str, str] = {
    "genshin": "miyoushe act_calendar",
    "starrail": "miyoushe get_act_calender",
    "zzz": "miyoushe activity_calendar",
    "arknights": "ak.hypergryph.com news(tab=1)+正文",
}

#: 新增游戏时在这里挂一个 fetch 函数即可。
FETCHERS = {
    "genshin": fetch_genshin_activities,
    "starrail": fetch_starrail_activities,
    "zzz": fetch_zzz_activities,
    "arknights": fetch_arknights_activities,
}


def refresh(force: bool = False) -> Dict[str, Any]:
    """刷新缓存（默认 20 小时内不重复请求）。失败保留旧数据，只记 last_error。"""
    cache = load_cache()
    age = _cache_age_hours(cache.get("fetched_at", ""))
    if not force and cache.get("version") == CACHE_VERSION and age is not None and age < CACHE_TTL_HOURS:
        return summary()

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    collected: List[Dict[str, Any]] = []
    errors: List[str] = []
    for game_id, fetcher in FETCHERS.items():
        try:
            collected.extend(fetcher())
        except Exception as exc:  # noqa: BLE001 - 单个游戏失败不能影响其它游戏
            logger.warning("拉取 %s 活动日历失败：%s", game_id, exc)
            errors.append(f"{game_id}: {exc}")

    cache["last_attempt"] = now_text
    if errors and not collected:
        cache["last_error"] = "；".join(errors)
        save_cache(cache)
        logger.warning("活动缓存未更新（沿用旧数据）：%s", cache["last_error"])
        return summary()

    # 黑名单只做「读时过滤」：缓存里保留全量，
    # 这样点「恢复全部」能立刻回来，不用等下一轮重新拉接口。
    cache["version"] = CACHE_VERSION
    cache["items"] = collected
    cache["fetched_at"] = now_text
    cache["source"] = dict(SOURCES)
    if errors:
        cache["last_error"] = "部分游戏拉取失败：" + "；".join(errors)
    else:
        cache.pop("last_error", None)
    cache["notified"] = _prune_notified(cache.get("notified") or {})
    save_cache(cache)
    ignored = _ignored_ids()
    hidden = sum(
        1 for item in collected
        if f"{item['game']}:{item.get('kind', 'act')}:{item.get('remote_id') or item['title']}" in ignored
    )
    logger.info(
        "活动日历已更新：%d 个进行中（其中 %d 个在黑名单里，不展示不提醒）",
        len(collected), hidden,
    )
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
    """当前仍在进行中 / 即将开始的活动（只读缓存，不发请求）。"""
    cache = load_cache()
    now = datetime.now()
    ignored = _ignored_ids()
    result: List[Activity] = []
    for item in cache.get("items") or []:
        if not isinstance(item, dict):
            continue
        start = _parse_time(item.get("start"))
        end = _parse_time(item.get("end"))
        if not start or not end or end <= now:
            continue
        activity = Activity(
            game_id=str(item.get("game") or ""),
            game_name=str(item.get("game_name") or ""),
            title=str(item.get("title") or ""),
            start=start,
            end=end,
            kind=str(item.get("kind") or "act"),
            status=str(item.get("status") or ""),
            progress=str(item.get("progress") or ""),
            icon=str(item.get("icon") or ""),
            remote_id=str(item.get("remote_id") or ""),
        )
        if activity.uid in ignored:
            continue  # 黑名单兜底：即使缓存里还在，也不让它出现
        result.append(activity)
    result.sort(key=lambda act: act.end)
    return result


def pending_alerts(
    hours: int = ALERT_WINDOW_HOURS,
    *,
    today: Optional[datetime] = None,
) -> List[ActivityAlert]:
    """剩余 <= hours 且已开始的活动，今天还没提醒过的。"""
    day_key = (today or datetime.now()).strftime("%Y-%m-%d")
    cache = load_cache()
    raw_notified = cache.get("notified", {}).get(day_key, [])
    notified = set(raw_notified) if isinstance(raw_notified, list) else set()
    alerts: List[ActivityAlert] = []
    for act in ongoing():
        if act.not_started:
            continue
        left = act.hours_left
        if not (0 < left <= hours):
            continue
        if act.uid in notified:
            continue
        alerts.append(ActivityAlert(
            game_id=act.game_id, game_name=act.game_name, title=act.title,
            end=act.end, uid=act.uid,
        ))
    return alerts


def mark_notified(alerts: Iterable[ActivityAlert], *, today: Optional[datetime] = None) -> None:
    """把今天已经推送过的活动记下来，避免同一天每轮检查都催一遍。"""
    day_key = (today or datetime.now()).strftime("%Y-%m-%d")
    ids = [alert.uid for alert in alerts if alert.uid]
    if not ids:
        return
    with _IGNORED_WRITE_LOCK:
        cache = load_cache()
        notified = cache.get("notified") or {}
        if not isinstance(notified, dict):
            notified = {}
        raw_current = notified.get(day_key)
        current = list(raw_current) if isinstance(raw_current, list) else []
        for uid in ids:
            if uid not in current:
                current.append(uid)
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
    soon = [item for item in activities if item.get("ending_soon")]
    ignored = load_ignored()
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
        "soon_count": len(soon),
        "ignored_count": len(ignored.get("ids") or []),
    }
