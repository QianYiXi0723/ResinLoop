#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形化扫码登录界面（本地 Web 服务，只监听 127.0.0.1）。

选哪些游戏就登录哪些：
  - 米游社：一次扫码，勾选的游戏同时更新（当然可以只勾一款）
  - 森空岛：每款需要单独扫码，勾几款就依次扫几次

为什么用网页而不是 tkinter：
  - 二维码要足够大才能扫得稳，浏览器缩放/居中天然合适；
  - 只需要标准库 + 已有的 qrcode / pillow，不新增任何依赖；
  - 手机扫码时电脑屏幕不用凑近看。

用法：
    python login_ui.py                  # 启动并自动打开浏览器
    python login_ui.py --port 8800      # 指定端口
    python login_ui.py --no-browser     # 不自动开浏览器

登录成功后直接写 config.json，只动被勾选的游戏。
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
CONFIG_PATH = BASE_DIR / "config.json"

#: 静态资源（游戏头像等），只允许读这个目录下的图片
STATIC_DIR = BASE_DIR / "static"
STATIC_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

#: 页面里引用图标的游戏（顺序即 fallback 顺序）
ICON_GAMES = ("genshin", "starrail", "zzz", "arknights")
#: 图标目录（/static/icons/... 对应的真实路径）
ICON_DIR = STATIC_DIR / "icons"


def _icon_stamp() -> str:
    """图标缓存戳：取四张图最新的 mtime。

    换一张图，时间戳就变，页面里的 URL 变成 `...png?v=1793xxxx`，
    浏览器/WebView2 当成新资源重新下载 —— 这是治「换了图还显示旧图」的唯一可靠办法。
    """
    stamp = 0
    for game_id in ICON_GAMES:
        try:
            stamp = max(stamp, int((ICON_DIR / f"{game_id}.png").stat().st_mtime))
        except OSError:
            continue
    return str(stamp)


def _render_page() -> str:
    """把模板里的 __ICON_STAMP__ 换成当前缓存戳。每次请求都算，不缓存。"""
    return HTML_PAGE.replace("__ICON_STAMP__", _icon_stamp())

logger = logging.getLogger("dailycheck.loginui")

#: 米游社三款游戏：id -> 显示名
MIHOYO_GAMES: Dict[str, str] = {
    "genshin": "原神",
    "starrail": "崩坏：星穹铁道",
    "zzz": "绝区零",
}

#: 森空岛两款游戏：id -> 显示名
SKLAND_TARGETS: Dict[str, str] = {
    "arknights": "明日方舟",
    "endfield": "明日方舟：终末地",
}

#: 错过时间点后的补发窗口（分钟）：0 = 关闭补发
CATCH_UP_MINUTES_DEFAULT = 180
CATCH_UP_MINUTES_MAX = 720


# --------------------------------------------------------------------------- #
# 会话状态
# --------------------------------------------------------------------------- #


class Session:
    """一次登录任务的完整状态。

    state 取值：creating / waiting / scanned / writing / done / error
    森空岛是多步的（每款一次扫码），用 step / total / current 表示进度。
    """

    def __init__(self, provider: str, targets: List[str]) -> None:
        self.sid = uuid.uuid4().hex
        self.provider = provider
        self.targets = list(targets)
        self.total = len(self.targets)
        self.step = 0            # 已完成的款数
        self.current = ""        # 正在登录的那一款
        self.state = "creating"
        self.message = "正在准备…"
        self.qr = ""
        self.detail: Dict[str, Any] = {}
        self.results: List[Dict[str, Any]] = []
        self.created_at = time.time()
        self._lock = threading.Lock()

    def labels(self) -> List[str]:
        table = MIHOYO_GAMES if self.provider == "mihoyo" else SKLAND_TARGETS
        return [table.get(t, t) for t in self.targets]

    def update(self, state: Optional[str] = None, message: Optional[str] = None,
               **detail: Any) -> None:
        with self._lock:
            if state:
                self.state = state
            if message:
                self.message = message
            if detail:
                self.detail.update(detail)

    def set_qr(self, png_b64: str) -> None:
        with self._lock:
            self.qr = png_b64

    def add_result(self, game: str, label: str, ok: bool, note: str = "") -> None:
        with self._lock:
            self.results.append({"game": game, "label": label, "ok": ok, "note": note})

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            table = MIHOYO_GAMES if self.provider == "mihoyo" else SKLAND_TARGETS
            return {
                "sid": self.sid,
                "provider": self.provider,
                "targets": list(self.targets),
                "labels": [table.get(t, t) for t in self.targets],
                "step": self.step,
                "total": self.total,
                "current": self.current,
                "currentLabel": table.get(self.current, self.current),
                "state": self.state,
                "message": self.message,
                "qr": self.qr,
                "detail": dict(self.detail),
                "results": list(self.results),
            }


SESSIONS: Dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def _put(session: Session) -> None:
    with SESSIONS_LOCK:
        cutoff = time.time() - 600
        for sid in [k for k, v in SESSIONS.items() if v.created_at < cutoff]:
            SESSIONS.pop(sid, None)
        SESSIONS[session.sid] = session


def _get(sid: str) -> Optional[Session]:
    with SESSIONS_LOCK:
        return SESSIONS.get(sid)


# --------------------------------------------------------------------------- #
# 二维码
# --------------------------------------------------------------------------- #


def qr_png_base64(text: str) -> str:
    """把文本渲染成 PNG 二维码并转 base64；缺依赖时返回空串（前端会降级提示）。"""
    try:
        import qrcode  # noqa: PLC0415 - 可选依赖
    except ImportError:
        logger.warning("未安装 qrcode，无法渲染二维码")
        return ""
    try:
        qr = qrcode.QRCode(border=2, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#111111", back_color="#ffffff")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("二维码渲染失败：%s", exc)
        return ""


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #


def ensure_config() -> None:
    """没有 config.json 就按默认值生成一份（幂等）。

    只为让「下载后直接扫码」少一步：不用先手动跑 python main.py --init。
    """
    if CONFIG_PATH.exists():
        return
    from core.config import create_default_config  # 复用同一份默认值，避免两处漂移

    create_default_config(CONFIG_PATH)
    logger.info("未找到 config.json，已按默认值生成：%s", CONFIG_PATH)


def load_raw_config() -> Dict[str, Any]:
    ensure_config()
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _enabled_game_ids() -> List[str]:
    """当前 config.json 里 enabled=true 的游戏 id（页面要列游戏的唯一来源）。

    报表、日志的"有哪些游戏"全部从这里派生，**任何地方都不再写死游戏清单**，
    这样将来加第七款游戏时只要注册一次就到处可见。读不到配置就返回空列表，
    不让一个坏配置把整个页面拖垮。
    """
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001 - 同上
        return []
    games: List[str] = []
    for item in (raw.get("accounts") or []):
        if not isinstance(item, dict):
            continue
        game = str(item.get("game") or "").strip()
        if game and item.get("enabled", True) and game not in games:
            games.append(game)
    return games


def push_status() -> Dict[str, Any]:
    """当前推送通道的配置状态。

    扫码只解决「能查到数据」，收不收得到通知取决于推送通道是否填了 key。
    页面据此提示，避免出现「扫码成功却始终收不到提醒」的困惑。
    """
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001 - 读不到配置也要能开页面
        return {"channels": [], "ready": [], "configured": False}

    push = raw.get("push") or {}
    channels = [str(c).lower() for c in (push.get("channels") or [])]
    ready: List[str] = []
    for name in channels:
        node = push.get(name) or {}
        if not isinstance(node, dict):
            continue
        if name == "bark" and str(node.get("device_key") or "").strip():
            ready.append(name)
        elif name == "serverchan" and str(node.get("sendkey") or "").strip():
            ready.append(name)
        elif name == "qmsg" and str(node.get("key") or "").strip():
            ready.append(name)
        elif name in ("feishu", "dingtalk") and str(node.get("webhook") or "").strip():
            ready.append(name)
    return {
        "channels": channels,
        "ready": ready,
        "configured": bool(ready),
        "always_notify": bool(push.get("always_notify", False)),
    }


def _game_display_map() -> Dict[str, str]:
    """游戏 id -> 中文名，唯一来源是 core.config.KNOWN_GAMES。"""
    try:
        from core.config import KNOWN_GAMES

        return dict(KNOWN_GAMES)
    except Exception:  # noqa: BLE001
        return {}


def _enabled_account_info() -> List[Dict[str, str]]:
    """当前参与检查的账号：id / 中文名 / 昵称（页面展示用）。"""
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001
        return []
    names = _game_display_map()
    items: List[Dict[str, str]] = []
    seen: set = set()
    for node in (raw.get("accounts") or []):
        if not isinstance(node, dict):
            continue
        game = str(node.get("game") or "").strip()
        if not game or game in seen:
            continue
        seen.add(game)
        if not node.get("enabled", True):
            continue
        items.append({
            "id": game,
            "name": names.get(game, game),
            "nickname": str(node.get("nickname") or ""),
        })
    return items


def accounts_status() -> Dict[str, Any]:
    """每个游戏账号的当前状态：是否参与巡检（enabled）、是否已有登录凭证。

    这是「账号绑定」页复选框的唯一事实来源：勾选 = 参与每日巡检并推送，
    与扫码登录（凭证更新）是两件独立的事。
    """
    names = _game_display_map()
    items: List[Dict[str, Any]] = []
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001
        return {"ok": False, "items": []}
    for node in (raw.get("accounts") or []):
        if not isinstance(node, dict):
            continue
        game = str(node.get("game") or "").strip()
        if not game:
            continue
        cred = node.get("credential") if isinstance(node.get("credential"), dict) else {}
        has_cred = bool(str(cred.get("cookie") or "").strip()
                        or str(cred.get("token") or "").strip())
        items.append({
            "id": game,
            "name": names.get(game, game),
            "nickname": str(node.get("nickname") or ""),
            "enabled": bool(node.get("enabled", True)),
            "has_credential": has_cred,
            "credential_updated_at": str(cred.get("updated_at") or ""),
        })
    return {"ok": True, "items": items}


def set_account_enabled(games: List[str], enabled: bool) -> Dict[str, Any]:
    """把指定游戏写进/移出每日巡检（config accounts[].enabled）。

    只动 enabled，绝不碰 credential —— 提醒开关和登录凭证彻底解耦。
    """
    if not games:
        raise ValueError("没有指定游戏")
    raw = load_raw_config()
    names = _game_display_map()
    present = {
        str(a.get("game") or "").strip(): a
        for a in (raw.get("accounts") or [])
        if isinstance(a, dict)
    }
    changed: List[str] = []
    for game in games:
        node = present.get(game)
        if node is None:
            continue
        node["enabled"] = bool(enabled)
        changed.append(str(node.get("nickname") or names.get(game, game)))
    if not changed:
        raise ValueError("config.json 里找不到这些游戏节点")
    save_raw_config(raw)
    if enabled:
        message = "已开启提醒：" + "、".join(changed)
    else:
        message = "已关闭提醒：" + "、".join(changed)
    return {"ok": True, "enabled": bool(enabled), "games": changed, "message": message}


# --------------------------------------------------------------------------- #
# 体力溢出阈值（/api/settings）：用户在 UI 改完直接写 config.app
# --------------------------------------------------------------------------- #

#: 前端字段名 -> (写到 config.app 的 JSON 键, 默认值)。跟 core/config 字段名保持一致。
_THRESHOLD_FIELDS: Tuple[Tuple[str, str, int], ...] = (
    ("genshin",   "genshin_resin_threshold",    160),
    ("starrail",  "starrail_stamina_threshold", 240),
    ("zzz",       "zzz_energy_threshold",       200),
    ("arknights", "arknights_ap_threshold",     135),
    ("endfield",  "endfield_sanity_threshold",  300),
)


def _resource_thresholds() -> Dict[str, int]:
    """读当前 config.json 里的三个阈值，缺字段按默认值返回；0 = 已关闭。"""
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001
        return {key: default for key, _jk, default in _THRESHOLD_FIELDS}
    app = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    out: Dict[str, int] = {}
    for key, json_key, default in _THRESHOLD_FIELDS:
        value = app.get(json_key)
        if value is None or (isinstance(value, str) and not value.strip()):
            out[key] = default
        else:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = default
    return out


def _normalize_threshold(raw: Any, label: str, default: int) -> int:
    """阈值校验：空 -> 默认；非法 -> 抛 ValueError；0=关闭；最大 1000。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是 0（关闭）或正整数，收到：{raw!r}")
    if value < 0:
        return 0
    return min(value, 1000)


def _resource_alert_enabled() -> bool:
    """体力溢出提醒总开关；读不到配置时按开启处理（保持旧行为）。"""
    try:
        raw = load_raw_config()
    except Exception:  # noqa: BLE001
        return True
    app = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    try:
        return _normalize_bool(app.get("resource_alert_enabled"), True)
    except ValueError:
        return True


def thresholds_status() -> Dict[str, Any]:
    """返回当前阈值 + 默认值 + 上限 + 总开关（前端占位符、提示用）。"""
    return {
        "ok": True,
        "enabled": _resource_alert_enabled(),
        "values": _resource_thresholds(),
        "defaults": {key: default for key, _jk, default in _THRESHOLD_FIELDS},
        "max": 1000,
    }


#: config.json 是「读 → 改 → 写」的共享文件。界面上两个开关几乎同时保存时，
#: 后写的一方会拿着旧副本把前一次的改动覆盖掉。这把锁把整段临界区串起来。
_CONFIG_WRITE_LOCK = threading.Lock()


def _serialized_write(func):
    """让同一个进程内的配置写入排队执行（HTTP 服务是多线程的）。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _CONFIG_WRITE_LOCK:
            return func(*args, **kwargs)

    return wrapper


@_serialized_write
def save_thresholds(payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存三个阈值。校验不过抛 ValueError（接口返 400，不落盘）。"""
    from core.config import parse_config

    normalized: Dict[str, int] = {}
    problems: List[str] = []
    for key, _json_key, default in _THRESHOLD_FIELDS:
        raw = payload.get(key) if isinstance(payload, dict) else None
        try:
            normalized[key] = _normalize_threshold(raw, key, default)
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("；".join(problems))

    # 总开关（可选）：不传就保持原样，避免只改阈值时把开关状态覆盖掉
    enabled = None
    if isinstance(payload, dict) and payload.get("enabled") is not None:
        enabled = _normalize_bool(payload.get("enabled"), True)

    raw = load_raw_config()
    if not isinstance(raw.get("app"), dict):
        raw["app"] = {}
    if enabled is not None:
        raw["app"]["resource_alert_enabled"] = enabled
    raw["app"]["genshin_resin_threshold"]    = normalized["genshin"]
    raw["app"]["starrail_stamina_threshold"] = normalized["starrail"]
    raw["app"]["zzz_energy_threshold"]       = normalized["zzz"]
    raw["app"]["arknights_ap_threshold"]     = normalized["arknights"]
    raw["app"]["endfield_sanity_threshold"]  = normalized["endfield"]

    # 用 core.config 自己的规则做全量复检 —— 校验不过就抛异常，文件保持原样
    parse_config(raw, source=CONFIG_PATH)
    save_raw_config(raw)
    logger.info(
        "体力溢出阈值已更新：原粹树脂=%d / 开拓力=%d / 电量=%d / 方舟理智=%d / 终末地理智=%d",
        normalized["genshin"], normalized["starrail"],
        normalized["zzz"], normalized["arknights"], normalized["endfield"],
    )

    result = thresholds_status()
    off = [k for k, v in normalized.items() if v == 0]
    if not result["enabled"]:
        result["message"] = "已保存阈值，但提醒总开关是关闭的（判定已跳过）"
    elif off:
        result["message"] = f"已保存（{', '.join(off)} 已关闭）"
    else:
        result["message"] = "已保存，下次检查自动生效"
    return result


def save_raw_config(raw: Dict[str, Any]) -> None:
    """写回 config.json：先序列化到临时文件并复检，再原子替换。

    直接 write_text 覆盖有个隐患——写到一半断电/被杀，config.json 就成半截
    JSON，之后所有命令都读不了配置。这里改成「临时文件 + os.replace」：
    同一分区内的 replace 是原子操作，失败时原文件完好无损。

    临时文件名用 mkstemp 保证唯一：界面上两个开关几乎同时保存时，
    固定文件名会让两个线程互相删掉对方的临时文件（先写的那次拿不到
    自己的 tmp，finally 清理直接抛错，于是配置写进去了、接口却返回 500）。
    """
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".tmp", dir=str(CONFIG_PATH.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(tmp.read_text(encoding="utf-8"))  # 复检：确保落盘内容可解析
        os.replace(tmp, CONFIG_PATH)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # 清理失败不影响已完成的保存
                pass



# --------------------------------------------------------------------------- #
# 通知绑定（push 节点读写）
# --------------------------------------------------------------------------- #

#: 通知绑定页管理的两个通道：name -> 显示名 / config.json 里的 Key 字段名
PUSH_CHANNELS: Dict[str, Dict[str, str]] = {
    "serverchan": {"label": "Server酱", "field": "sendkey"},
    "qmsg": {"label": "Qmsg 酱", "field": "key"},
}

#: 保存动作 -> 提示文案
ACTION_TEXT: Dict[str, str] = {
    "keep": "保持不变",
    "set": "已更新",
    "clear": "已删除",
    "unchanged": "未提交",
}


def mask_key(value: str, head: int = 4, tail: int = 3) -> str:
    """把 Key 打成 SCT4*******c41 这种掩码，仅用于页面回显。

    中间固定 7 个星号，不按真实长度铺星号——长度本身也是信息。
    掩码是「是否改动」的判定依据：前端原样回传掩码，后端就知道用户没动过。
    """
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * 8
    return value[:head] + "*" * 7 + value[-tail:]


def _push_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    """取出 push 节点；类型不对时**不静默覆盖**，交由调用方决定。"""
    push = raw.get("push")
    return push if isinstance(push, dict) else {}


def push_detail() -> Dict[str, Any]:
    """页面可安全展示的推送配置：只有掩码，绝不含明文 Key。"""
    try:
        raw = load_raw_config()
    except Exception as exc:  # noqa: BLE001 - 读不到也要能开页面
        return {"ok": False, "message": f"读取 config.json 失败：{exc}"}

    push = _push_node(raw)
    channels = [
        str(c).strip().lower() for c in (push.get("channels") or []) if str(c).strip()
    ]

    items: Dict[str, Any] = {}
    for name, meta in PUSH_CHANNELS.items():
        node = push.get(name)
        node = node if isinstance(node, dict) else {}
        key = str(node.get(meta["field"]) or "").strip()
        items[name] = {
            "label": meta["label"],
            "field": meta["field"],
            "enabled": bool(node.get("enabled", True)),
            "has_key": bool(key),
            "masked": mask_key(key),
            "in_channels": name in channels,
        }

    return {
        "ok": True,
        "channels": channels,
        "items": items,
        "always_notify": bool(push.get("always_notify", False)),
        "configured": any(i["has_key"] and i["in_channels"] for i in items.values()),
    }


def _resolve_key(submitted: str, existing: str) -> Tuple[str, str]:
    """判断这次提交对某个 Key 的意图：(keep | set | clear, 最终值)。

    - 空串        -> clear（清空 = 删除，并退出 channels）
    - 等于当前掩码 -> keep（用户没动输入框，绝不能覆盖原值）
    - 其它         -> set
    """
    text = (submitted or "").strip()
    if not text:
        return "clear", ""
    if existing and text == mask_key(existing):
        return "keep", existing
    if "*" in text:
        raise ValueError("输入内容里有 * 号，看起来是掩码而不是真实 Key，请粘贴完整 Key")
    return "set", text


def update_push_config(raw: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """把表单值合并进 raw["push"]（原地修改），返回动作、是否真有改动、新 channels。

    只碰这些位置，其余字段（timeout / server / bark / feishu…）原样保留：
        push.channels
        push.serverchan.{enabled, sendkey}
        push.qmsg.{enabled, key}
    """
    push = raw.get("push")
    if not isinstance(push, dict):
        push = {}
        raw["push"] = push

    before = json.loads(json.dumps(push))  # 深拷贝一份，用于判断到底改没改

    # 平安通知：全部日常完成时是否也发一条（默认关闭 = 无事不打扰）
    if "always_notify" in payload:
        push["always_notify"] = bool(payload.get("always_notify"))

    old_channels = [
        str(c).strip().lower() for c in (push.get("channels") or []) if str(c).strip()
    ]

    actions: Dict[str, str] = {}
    qualifying: Dict[str, bool] = {}

    for name, meta in PUSH_CHANNELS.items():
        field = meta["field"]
        item = payload.get(name)

        # 前端没提交这个通道：完全不动它
        if not isinstance(item, dict):
            node = push.get(name)
            node = node if isinstance(node, dict) else {}
            key = str(node.get(field) or "").strip()
            qualifying[name] = (
                bool(key) and bool(node.get("enabled", True)) and name in old_channels
            )
            actions[name] = "unchanged"
            continue

        node = push.get(name)
        if not isinstance(node, dict):
            node = {}
            push[name] = node

        existing = str(node.get(field) or "").strip()

        if "enabled" in item:
            node["enabled"] = bool(item.get("enabled"))
        enabled = bool(node.get("enabled", True))

        if "key" in item:
            action, value = _resolve_key(str(item.get("key") or ""), existing)
        else:
            action, value = "keep", existing

        if action == "clear":
            node[field] = ""
        elif action == "set":
            node[field] = value
        # keep：一个字节都不改，原 Key 原样留着

        final_key = str(node.get(field) or "").strip()
        qualifying[name] = enabled and bool(final_key)
        actions[name] = action

    # 重建 channels：其它通道保持原顺序；本次管理的两个通道按「启用且有 Key」决定去留
    final: List[str] = []
    for name in old_channels:
        if name in PUSH_CHANNELS:
            if qualifying.get(name) and name not in final:
                final.append(name)
        elif name not in final:
            final.append(name)
    for name in PUSH_CHANNELS:
        if qualifying.get(name) and name not in final:
            final.append(name)
    push["channels"] = final

    return {
        "actions": actions,
        "changed": json.loads(json.dumps(push)) != before,
        "channels": list(final),
    }


def run_test_push(payload: Dict[str, Any]) -> Dict[str, Any]:
    """用**当前表单值**发一条测试消息。

    只在内存里的配置副本上套用表单值，**不写盘**：想留下来必须先点「保存配置」。
    这样用户可以先试通再决定保存，也不会因为一次误操作改动 config.json。
    """
    try:
        from core.config import PushConfig
        from core.notifier import build_notifiers, push_all
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"推送模块加载失败：{exc}"}

    try:
        raw = json.loads(json.dumps(load_raw_config()))  # 深拷贝：与磁盘完全隔离
        update_push_config(raw, payload)
        push_cfg = PushConfig.from_dict(raw.get("push") or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"配置校验未通过：{exc}"}

    try:
        notifiers = build_notifiers(push_cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"构造推送器失败：{exc}"}

    ready = [n for n in notifiers if n.ready]
    if not ready:
        selected = [
            PUSH_CHANNELS[n.name]["label"] for n in notifiers if n.name in PUSH_CHANNELS
        ]
        tail = f"（当前选中：{'、'.join(selected)}）" if selected else ""
        return {
            "ok": False,
            "message": "没有可用的推送通道：请勾选「启用」并填入 Key" + tail,
        }

    now = time.strftime("%m-%d %H:%M")
    body = (
        "收到这条说明推送通道工作正常。\n"
        f"发送时间：{now}\n"
        "来源：登录界面 · 通知绑定 · 测试推送"
    )
    results = push_all(ready, "ResinLoop · 测试推送", body)
    rows = [
        {
            "channel": r.channel,
            "label": PUSH_CHANNELS.get(r.channel, {}).get("label", r.channel),
            "ok": bool(r.ok),
            "skipped": bool(r.skipped),
            "message": r.message,
        }
        for r in results
    ]
    logger.info(
        "测试推送：%s",
        "；".join(f"{x['label']}={'成功' if x['ok'] else '失败'}" for x in rows),
    )
    return {"ok": True, "results": rows}


# --------------------------------------------------------------------------- #
# 设备伪装
# --------------------------------------------------------------------------- #

#: 指纹缓存：换设备档案后必须删掉，否则会继续用旧机型的 device_fp
_DEVICE_CACHE_PATH = BASE_DIR / "logs" / "device_fp.json"

#: APK 下载服务信息（main() 启动后填充）
TOOLS_SERVER: Optional[Dict[str, Any]] = None

#: 启动失败的原因。空串表示没失败（要么没试过，要么成功了）
TOOLS_ERROR: str = ""

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _mask_oaid(value: str) -> str:
    text = (value or "").strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}****{text[-4:]}"


def apk_info(*, retry: bool = True) -> Dict[str, Any]:
    """tools/ 里的 APK 信息 + 手机扫码下载地址。

    ``retry=True``（默认）：服务没起来就**现在再试一次**。
    这样即使启动时端口被占 / tools 目录当时还是空的，等页面加载、
    用户把 APK 放进来或关掉占端口的程序之后，刷一下就能出来二维码，
    不用重启整个控制台。
    """
    global TOOLS_SERVER

    from core import tools_server

    if not TOOLS_SERVER and retry:
        start_tools_service()

    if not TOOLS_SERVER:
        return {
            "available": False,
            "message": TOOLS_ERROR or "下载服务未启动",
            "retryable": True,
        }

    files = [
        item for item in tools_server.list_files() if item["name"].lower().endswith(".apk")
    ]
    if not files:
        return {
            "available": False,
            "message": f"tools/ 目录里没有 APK（{tools_server.TOOLS_DIR}）",
            "retryable": True,
        }

    apk = files[0]
    url = TOOLS_SERVER["url"] + quote(apk["name"])
    info = {
        "available": True,
        "name": apk["name"],
        "size": apk["size"],
        "size_text": _human_size(apk["size"]),
        "url": url,
        "ip": TOOLS_SERVER["ip"],
        "port": TOOLS_SERVER["port"],
        "qr": qr_png_base64(url),
    }
    if TOOLS_SERVER["ip"] in ("127.0.0.1", "::1"):
        # 没拿到局域网地址：服务起来了，但手机扫了也下载不了
        info["warning"] = "没检测到局域网地址（可能没连 WiFi），手机扫码可能打不开"
    return info


def device_status() -> Dict[str, Any]:
    """当前生效的设备档案（OAID 打码）与相关状态。"""
    from core import mihoyo_api

    profile = mihoyo_api.load_device_profile()  # 读盘，换完立刻反映
    return {
        "ok": True,
        "custom": mihoyo_api.PROFILE_OVERRIDE_PATH.exists(),
        "profile": {
            "brand": profile.get("brand", ""),
            "model": profile.get("model", ""),
            "os_version": profile.get("os_version", ""),
            "sdk_version": profile.get("sdk_version", ""),
            "device": profile.get("device", ""),
            "device_info": profile.get("device_info", ""),
            "oaid": _mask_oaid(profile.get("oaid", "")),
        },
        "ua": mihoyo_api.build_user_agent(profile),
        "cache_exists": _DEVICE_CACHE_PATH.exists(),
        "apk": apk_info(),
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    """从粘贴内容里截出 JSON 对象。

    小白常常连着"设备信息"几个字一起复制，严格 json.loads 会失败，
    这里按首尾花括号截取，容错更高。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没找到 JSON 内容（应当以 { 开头、} 结尾）")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("粘贴的内容不是 JSON 对象")
    return data


def save_device_text(raw_text: str) -> Dict[str, Any]:
    """解析用户粘贴的手机档案 -> 写入覆盖文件 -> 清指纹缓存。"""
    from core import mihoyo_api

    text = (raw_text or "").strip()
    if not text:
        raise ValueError("请先粘贴「设备信息」App 导出的内容")

    data = _extract_json_object(text)
    mihoyo_api.save_device_profile(data)   # 校验 + 落盘
    mihoyo_api.reload_device_profile()     # 让 UA 等缓存立刻跟上
    removed = mihoyo_api.clear_device_cache()

    result = device_status()
    result["removed_cache"] = removed
    result["message"] = "设备指纹已替换，请重新扫码"
    logger.info("设备档案已替换为 %s（UA 已同步）", result["profile"]["model"])
    return result


def reset_device_text() -> Dict[str, Any]:
    """恢复默认设备档案并清缓存。"""
    from core import mihoyo_api

    mihoyo_api.clear_device_profile()
    mihoyo_api.reload_device_profile()
    removed = mihoyo_api.clear_device_cache()

    result = device_status()
    result["removed_cache"] = removed
    result["message"] = "已恢复默认设备档案，请重新扫码"
    return result


# --------------------------------------------------------------------------- #
# 定时与守护进程
# --------------------------------------------------------------------------- #


def _normalize_times(times: Any) -> List[str]:
    if not isinstance(times, list):
        raise ValueError("times 必须是数组")
    result: List[str] = []
    for item in times:
        text = str(item).strip()
        if not _TIME_RE.match(text):
            raise ValueError(f"「{text}」不是合法时间，格式应为 HH:MM（如 08:30）")
        if text not in result:
            result.append(text)
    if not result:
        raise ValueError("至少要保留一个时间点")
    if len(result) > 12:
        raise ValueError("时间点最多 12 个")
    return sorted(result)


def daemon_status() -> Dict[str, Any]:
    try:
        import daemon as daemon_mod

        return daemon_mod.status()
    except Exception as exc:  # noqa: BLE001 - 状态读不到不能影响页面
        logger.warning("读取守护进程状态失败：%s", exc)
        return {"running": False, "error": str(exc), "check_times": [], "next_run": ""}


def _normalize_bool(raw: Any, default: bool) -> bool:
    """解析总开关：接受 true/false、1/0、on/off；留空走默认值。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"开关值必须是 true 或 false，收到：{raw!r}")


def schedule_status() -> Dict[str, Any]:
    from core.config import next_check_time

    raw = load_raw_config()
    app = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    times = [str(item) for item in (app.get("check_times") or [])]
    target = next_check_time(times)
    return {
        "ok": True,
        "times": times,
        "catch_up_minutes": _normalize_catch_up(app.get("catch_up_minutes")),
        "catch_up_enabled": _normalize_bool(app.get("catch_up_enabled"), True),
        "resource_alert_enabled": _normalize_bool(app.get("resource_alert_enabled"), True),
        "activity_alert_enabled": _normalize_bool(app.get("activity_alert_enabled"), True),
        "activity_alert_days": _normalize_alert_days(app.get("activity_alert_days")),
        "next_run": target.strftime("%Y-%m-%d %H:%M") if target else "",
        "daemon": daemon_status(),
        # 页面上要列账号就取这里，避免把游戏清单写死在多份 HTML / JS 里
        "enabled_games": _enabled_game_ids(),
        "accounts_info": _enabled_account_info(),
    }


def log_tail(n: int = 200) -> Dict[str, Any]:
    """读取巡检日志（logs/dailycheck.log）最后 n 行，给底部日志控制台。

    当天日志被零点切分后 dailycheck.log 可能为空或不存在，此时回退到最近的
    dailycheck.log.YYYY-MM-DD。key 在 notifier 源头已脱敏，可直接展示。
    """
    log_dir = BASE_DIR / "logs"
    path = log_dir / "dailycheck.log"
    try:
        if not path.exists():
            candidates = sorted(log_dir.glob("dailycheck.log.*"))
            path = candidates[-1] if candidates else path
        if not path.exists():
            return {"ok": True, "lines": [], "message": "还没有日志文件"}
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = [line.rstrip("\r\n") for line in lines[-n:]]
        return {"ok": True, "lines": tail}
    except Exception as exc:  # noqa: BLE001 - 前端要的是 200 + 错误文案，不是 500
        return {"ok": False, "message": f"读取日志失败：{exc}"}


def _normalize_catch_up(raw: Any) -> int:
    """补发窗口（分钟）：0 = 关闭；没配这个字段时按 180 分钟处理。

    非数字会直接报错（交给接口返回 400），不静默吞掉——避免用户以为保存成功了。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return CATCH_UP_MINUTES_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"补发窗口必须是 0（关闭）~ {CATCH_UP_MINUTES_MAX} 之间的整数，收到：{raw!r}"
        )
    if value < 0:
        return 0
    return min(value, CATCH_UP_MINUTES_MAX)


#: 活动结束提前提醒天数：1~30，默认 3（与 core.config 保持一致）
ACTIVITY_ALERT_DAYS_DEFAULT = 3
ACTIVITY_ALERT_DAYS_MAX = 30


def _normalize_alert_days(raw: Any) -> int:
    """提前提醒天数：留空/非法走默认 3，越界收敛到 1~30（读取路径不报错）。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ACTIVITY_ALERT_DAYS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ACTIVITY_ALERT_DAYS_DEFAULT
    return max(1, min(value, ACTIVITY_ALERT_DAYS_MAX))


@_serialized_write
def save_catch_up(raw_minutes: Any, enabled: Any = None) -> Dict[str, Any]:
    """保存补发窗口（可选同时改总开关）。改完不用重启守护进程。"""
    value = _normalize_catch_up(raw_minutes)
    raw = load_raw_config()
    if not isinstance(raw.get("app"), dict):
        raw["app"] = {}
    raw["app"]["catch_up_minutes"] = value
    if enabled is not None:
        raw["app"]["catch_up_enabled"] = _normalize_bool(enabled, True)

    from core.config import parse_config

    parse_config(raw, source=CONFIG_PATH)
    save_raw_config(raw)
    logger.info("补发窗口已更新：%d 分钟（总开关=%s）", value,
                "开" if raw["app"].get("catch_up_enabled", True) else "关")
    result = schedule_status()
    if not result["catch_up_enabled"]:
        result["message"] = "已关闭补发（补发窗口的数值已保留）"
    elif value == 0:
        result["message"] = "已关闭补发（补发窗口为 0）"
    else:
        result["message"] = f"错过时间点后 {value} 分钟内会补发"
    return result


@_serialized_write
def save_activity_alert(enabled: Any = None, days: Any = None) -> Dict[str, Any]:
    """活动结束提醒开关与提前天数（config.app.activity_alert_*）。

    enabled / days 任传其一或都传；只改一个字段时另一个保持原值。
    """
    raw = load_raw_config()
    if not isinstance(raw.get("app"), dict):
        raw["app"] = {}
    changed_switch = enabled is not None
    changed_days = days is not None and str(days).strip() != ""
    if not changed_switch and not changed_days:
        raise ValueError("没有要保存的内容")

    if changed_switch:
        raw["app"]["activity_alert_enabled"] = _normalize_bool(enabled, True)
    if changed_days:
        raw["app"]["activity_alert_days"] = _normalize_alert_days(days)
    value = _normalize_alert_days(raw["app"].get("activity_alert_days"))
    switch_on = _normalize_bool(raw["app"].get("activity_alert_enabled"), True)

    from core.config import parse_config

    parse_config(raw, source=CONFIG_PATH)
    save_raw_config(raw)
    logger.info("活动结束提醒：开关=%s，提前 %d 天", "开" if switch_on else "关", value)
    result = schedule_status()
    if changed_days:
        result["message"] = f"活动结束前 {value} 天开始提醒"
    elif switch_on:
        result["message"] = f"已开启活动结束提醒（结束前 {value} 天开始提醒）"
    else:
        result["message"] = "已关闭活动结束提醒"
    return result


# --------------------------------------------------------------------------- #
# 信息查询：实时体力 / 活动缓存
# --------------------------------------------------------------------------- #

#: 实时查询结果缓存（秒）。切页签不该每次都重新打 5 个游戏的接口。
_LIVE_TTL_SECONDS = 45
_LIVE_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}


def _split_progress(text: Any) -> Tuple[Optional[int], Optional[int]]:
    """把 "160/200" 拆成 (160, 200)；解析不了返回 (None, None)。"""
    if not isinstance(text, str) or "/" not in text:
        return None, None
    left, right = text.split("/", 1)
    try:
        return int(left.strip()), int(right.strip())
    except ValueError:
        return None, None


def live_status(force: bool = False) -> Dict[str, Any]:
    """跑一次实时查询，返回每款游戏的体力 / 理智看板。

    与定时检查共用 ``collect_statuses``：不写历史、不推送。结果在进程内缓存 45 秒，
    避免来回切页签时反复打接口。
    """
    cached = _LIVE_CACHE.get("payload")
    if (not force) and cached is not None and (time.time() - float(_LIVE_CACHE.get("at") or 0)) < _LIVE_TTL_SECONDS:
        payload = dict(cached)
        payload["cached"] = True
        return payload

    from core.config import RESOURCE_TASK_NAMES, load_config
    from main import collect_statuses

    config = load_config(CONFIG_PATH)
    statuses = collect_statuses(config)

    games: List[Dict[str, Any]] = []
    for status in statuses:
        want = RESOURCE_TASK_NAMES.get(status.game_id)
        resource: Optional[Dict[str, Any]] = None
        for task in status.tasks:
            if task.name != want:
                continue
            current, maximum = _split_progress(task.progress)
            percent = None
            if current is not None and maximum:
                percent = max(0, min(100, round(current * 100 / maximum)))
            resource = {
                "name": task.name,
                "progress": task.progress,
                "current": current,
                "maximum": maximum,
                "percent": percent,
            }
            break
        games.append({
            "game": status.game_id,
            "game_name": status.game_name,
            "nickname": status.nickname,
            "ok": status.ok,
            "finished": status.finished,
            "error": status.error or "",
            "resource": resource,
            # 判定任务明细（不含"仅展示"的资源项）：供迷你卡显示"委托 0/4"级进度
            "tasks": [
                {"name": t.name, "done": bool(t.done), "progress": t.progress or ""}
                for t in status.counted_tasks
            ],
        })

    payload = {
        "ok": True,
        "cached": False,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "games": games,
    }
    _LIVE_CACHE["at"] = time.time()
    _LIVE_CACHE["payload"] = payload
    return payload


@_serialized_write
def save_schedule(times: Any) -> Dict[str, Any]:
    normalized = _normalize_times(times)
    raw = load_raw_config()
    if not isinstance(raw.get("app"), dict):
        raw["app"] = {}
    raw["app"]["check_times"] = normalized

    # 写盘前用真正的配置解析器复检，保证不会把 config.json 写坏
    from core.config import parse_config

    parse_config(raw, source=CONFIG_PATH)
    save_raw_config(raw)

    result = schedule_status()
    result["message"] = f"已保存 {len(normalized)} 个时间点"
    logger.info("时间点已更新：%s", "、".join(normalized))
    return result


def write_mihoyo(cookie: str, games: List[str]) -> List[str]:
    """把 Cookie 写进**选中的**游戏节点，其余游戏原样不动。"""
    raw = load_raw_config()
    wanted = set(games)
    updated: List[str] = []
    for account in raw.get("accounts", []):
        game = account.get("game")
        if game in wanted:
            cred = account.setdefault("credential", {})
            cred["cookie"] = cookie
            cred["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            # 清掉可能残留的分字段，避免覆盖整串 cookie
            cred.pop("ltuid", None)
            cred.pop("ltoken", None)
            updated.append(str(account.get("nickname") or MIHOYO_GAMES.get(game, game)))
    present = {a.get("game") for a in raw.get("accounts", [])}
    missing = wanted - present
    if missing:
        raise RuntimeError(f"config.json 里找不到这些游戏节点：{', '.join(sorted(missing))}")
    if not updated:
        raise RuntimeError("没有写入任何游戏，请检查 config.json 的 accounts")

    query = raw.setdefault("query", {})
    query["mihoyo_enabled"] = True
    if str(query.get("provider", "")).strip().lower() == "mock":
        query["provider"] = "miyoushe"
    save_raw_config(raw)
    return updated


def write_skland(target: str, token: str) -> str:
    """只更新目标游戏的 token，不动另一款。"""
    raw = load_raw_config()
    hit = False
    for account in raw.get("accounts", []):
        if account.get("game") == target:
            cred = account.setdefault("credential", {})
            cred["token"] = token
            cred["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            hit = True
            break
    if not hit:
        raise RuntimeError(f"config.json 里没有 {target} 账号节点")

    query = raw.setdefault("query", {})
    if str(query.get("provider", "")).strip().lower() == "mock":
        query["provider"] = "miyoushe"
    save_raw_config(raw)

    # 清掉旧 cred 缓存，强制下次用新 token 重新换取
    (BASE_DIR / "logs" / f"skland_cred_{target}.json").unlink(missing_ok=True)
    return SKLAND_TARGETS.get(target, target)


# --------------------------------------------------------------------------- #
# 登录流程（后台线程）
# --------------------------------------------------------------------------- #


def run_mihoyo(session: Session) -> None:
    """一次扫码，更新所有被勾选的米游社游戏。"""
    from core.mihoyo_api import bind_device
    from core.mihoyo_login import (
        MiyousheLoginError,
        build_cookie_string,
        create_qr,
        poll_confirm,
    )

    games = list(session.targets)

    def on_status(status: str) -> None:
        if status == "Scanned":
            session.update("scanned", "已扫码，请在手机上点击「确认登录」")

    try:
        device_id = str(uuid.uuid4()).lower()
        session.update("creating", f"正在申请二维码（将更新 {len(games)} 款游戏）…")
        qr = create_qr(device_id)
        session.set_qr(qr_png_base64(qr["url"]))
        session.update("waiting", "请用手机「米游社」App 扫码")

        result = poll_confirm(device_id, qr["ticket"], timeout=180, on_status=on_status)

        session.update("writing", "扫码成功，正在绑定设备并写入配置…")
        cookie, creds = build_cookie_string(result["cookies"])
        # 只重签被勾选游戏的指纹，其余游戏保持原设备身份
        bind_device(device_id, games)
        updated = write_mihoyo(cookie, games)

        for game in games:
            session.add_result(game, MIHOYO_GAMES.get(game, game), True)
        session.update(
            "done",
            f"登录成功，已更新 {len(updated)} 款游戏",
            ltuid=creds["ltuid"],
            games=updated,
            note="设备指纹已绑定到这些游戏，可直接运行 python main.py 验证",
        )
        logger.info("米游社登录成功，ltuid=%s，写入 %s", creds["ltuid"], "、".join(updated))
    except MiyousheLoginError as exc:
        session.update("error", str(exc))
        logger.warning("米游社登录失败：%s", exc)
    except Exception as exc:  # noqa: BLE001
        session.update("error", f"登录异常：{exc}")
        logger.exception("米游社登录未预期异常")


def run_skland(session: Session) -> None:
    """逐款扫码：勾了几款就依次扫几次，每款单独写自己的 token。"""
    from core.hypergryph_login import (
        HypergryphLoginError,
        create_qr,
        exchange_short_token,
        poll_confirm,
    )
    from core.skland_api import verify_ark_token

    total = session.total
    finished: List[str] = []

    for index, target in enumerate(session.targets, 1):
        label = SKLAND_TARGETS.get(target, target)
        session.current = target

        def on_status(status: Any) -> None:
            if status == 101:
                session.update(
                    "scanned", f"第 {index}/{total} 步 · 已扫码，请在手机上点击「确认」"
                )

        try:
            session.set_qr("")
            session.update("creating", f"第 {index}/{total} 步 · {label}：正在申请二维码…")
            qr = create_qr()
            session.set_qr(qr_png_base64(qr["scan_url"]))
            session.update(
                "waiting",
                f"第 {index}/{total} 步 · 请用「森空岛」App 扫描{label}的二维码",
            )

            scan_code = poll_confirm(qr["scan_id"], timeout=180, on_status=on_status)

            session.update("writing", f"第 {index}/{total} 步 · 正在验证 {label} 的 Token…")
            token = exchange_short_token(scan_code)
            check = verify_ark_token(token)
            if not check.get("ok"):
                raise HypergryphLoginError(
                    f"Token 校验未通过：{check.get('message') or check}"
                )

            name = write_skland(target, token)
            session.add_result(target, name, True)
            finished.append(name)
            session.step = index
            logger.info("%s 登录成功（第 %d/%d 款）", name, index, total)
        except HypergryphLoginError as exc:
            session.add_result(target, label, False, str(exc))
            session.update(
                "error",
                f"第 {index}/{total} 步 · {label} 登录失败：{exc}"
                + (f"（已完成：{'、'.join(finished)}）" if finished else ""),
                finished=finished,
            )
            logger.warning("%s 登录失败：%s", label, exc)
            return
        except Exception as exc:  # noqa: BLE001
            session.add_result(target, label, False, f"登录异常：{exc}")
            session.update("error", f"第 {index}/{total} 步 · {label} 登录异常：{exc}")
            logger.exception("%s 登录未预期异常", label)
            return

    session.update(
        "done",
        f"全部完成，{len(finished)} 款游戏已更新",
        games=finished,
        note="各游戏的 cred 缓存已清除，下次运行自动换新",
    )


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ResinLoop · 控制台</title>
<style>
  /* ---------- 全局 Token（双主题：html[data-theme] 驱动，见 JS applyTheme） ---------- */
  :root{
    --urgency-warn:#d9a441; --urgency-critical:#e05252; --accent-blue:#5b9cf5;
  }
  /* 深色 Token 组 */
  html[data-theme="dark"]{
    --bg-base:#1a1a1a; --bg-sub:#141414; --bg-card:#252526; --bg-elevated:#252526;
    --bg-raise:#2d2d2d; --bg-head:#2b2b2b; --bg-side:#161616; --bg-input:#111111;
    --btn-bg:#333333; --btn-hover:#3d3d3d; --nav-on:#333333; --nav-on-fg:#ffffff;
    --focus-ring:rgba(74,155,232,.45);
    --card-shadow:inset 0 1px 0 rgba(255,255,255,0.04), 0 2px 8px rgba(0,0,0,0.35);
    --border-subtle:rgba(255,255,255,0.06); --border-strong:rgba(255,255,255,0.12);
    --text-secondary:#a0a0a0; --text-tertiary:#666666;
    --bg:var(--bg-base); --surface:var(--bg-card); --subtle:var(--bg-sub); --fg:#e8e8e8;
    --muted:var(--text-secondary); --line:var(--border-subtle); --input-line:#38383c;
    --hover:rgba(255,255,255,.06); --zebra:rgba(255,255,255,.03); --sel:#33333a;
    --ok-bg:rgba(74,186,138,.15); --err-bg:rgba(224,96,85,.15);
    --brand:#4a9be8; --brand-hover:#5dabf2; --ok:#4aba8a; --warn:#e0a040; --err:#e06055;
    --violet:#a78bfa;
  }
  /* 浅色 Token 组：背景/卡片浅色，--fg 深色 —— 黑字永远压浅底 */
  html[data-theme="light"]{
    --bg-base:#f4f6f9; --bg-sub:#eef0f4; --bg-card:#ffffff; --bg-elevated:#ffffff;
    --bg-raise:#ffffff; --bg-head:#f7f8fa; --bg-side:#ffffff; --bg-input:#f5f5f5;
    --btn-bg:#eef0f4; --btn-hover:#e2e6ec; --nav-on:#e8f0fe; --nav-on-fg:var(--brand);
    --focus-ring:rgba(11,103,194,.30);
    --card-shadow:0 2px 8px rgba(0,0,0,0.04);
    --border-subtle:rgba(0,0,0,0.08); --border-strong:rgba(0,0,0,0.14);
    --text-secondary:#5c636e; --text-tertiary:#9aa1ab;
    --bg:var(--bg-base); --surface:var(--bg-card); --subtle:var(--bg-sub); --fg:#1b1f24;
    --muted:var(--text-secondary); --line:var(--border-subtle); --input-line:#e2e5e9;
    --hover:rgba(0,0,0,.045); --zebra:rgba(0,0,0,.018); --sel:#e7eef8;
    --ok-bg:rgba(10,125,67,.10); --err-bg:rgba(192,57,43,.10);
    --brand:#0b67c2; --brand-hover:#0857a5; --ok:#0a7d43; --warn:#b45309; --err:#c0392b;
    --violet:#6d3fc0;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--fg);
       font:12.5px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}

  /* ---------- 布局：整体纵向（上方=侧栏+内容，底部=日志台），日志不再挤占/悬浮 ---------- */
  .app{height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .app-body{flex:1;display:flex;min-height:0}
  .side{width:200px;flex:none;background:var(--bg-side);
        display:flex;flex-direction:column;padding:10px 8px}
  .brand{display:flex;align-items:center;gap:8px;padding:6px 8px 12px}
  .logo{width:26px;height:26px;border-radius:4px;background:var(--brand);color:#fff;
        display:grid;place-items:center;font-weight:700;font-size:13px;flex:none}
  .brand b{display:block;font-size:13.5px;line-height:1.2}
  .brand span{font-size:11px;color:var(--muted)}
  .nav{display:flex;flex-direction:column;gap:2px}
  .nav button{display:flex;align-items:center;gap:9px;width:100%;text-align:left;
        padding:9px 12px;border-radius:6px;
        border:none;background:transparent;color:var(--fg);font-size:13px;cursor:pointer;
        position:relative;white-space:nowrap}
  .nav button .nav-ico{font-size:13px;width:18px;text-align:center;flex:none}
  .nav button:hover{background:var(--hover)}
  .nav button.on{background:var(--nav-on);color:var(--nav-on-fg);font-weight:600}
  .nav button.on .nav-ico{color:var(--nav-on-fg)}
  .side-foot{margin-top:auto;padding:10px 8px 4px;
        font-size:11px;color:var(--muted)}
  .theme-btn{display:block;width:100%;text-align:left;padding:0;border:none;
        background:transparent;color:var(--muted);font-size:11px;cursor:pointer}
  .theme-btn:hover{color:var(--fg)}
  .main{flex:1;min-width:0;width:100%;padding:24px 32px;
        display:flex;flex-direction:column;min-height:0;overflow-y:auto}
  @media (max-width:760px){
    .app-body{flex-direction:column}
    .side{width:100%;flex-direction:row;align-items:center}
    .brand{padding:4px 8px}
    .nav{flex-direction:row;overflow-x:auto;scrollbar-width:none}
    .nav::-webkit-scrollbar{display:none}
    .side-foot{display:none}
    .main{padding:12px}
  }
  .tabpanel{display:none}
  /* 满幅仪表盘：12 列 Bento 网格，宽度由跨列吸收，绝不用 max-width 圈内容 */
  .tabpanel.on{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;
               align-items:start;margin-bottom:16px}
  .tabpanel.on > *{margin:0;min-width:0}
  .s3{grid-column:span 3}
  .s4{grid-column:span 4}
  .s5{grid-column:span 5}
  .s6{grid-column:span 6}
  .s7{grid-column:span 7}
  .s8{grid-column:span 8}
  .s9{grid-column:span 9}
  .s12{grid-column:span 12}
  @media (max-width:1000px){
    .tabpanel.on > *{grid-column:1/-1}
  }
  /* 每日报表页：表格区拉伸吃掉视口剩余高度 */
  #tab-history.on{align-items:stretch;flex:1;min-height:0;
                  grid-template-rows:auto minmax(0,1fr)}

  /* ---------- 卡片（Bento 基础卡）：--bg-card + 微光描边 + 写死阴影 ---------- */
  .card{background:var(--bg-card);border:none;border-radius:10px;
        box-shadow:var(--card-shadow);
        transition:box-shadow 400ms ease-out, padding 400ms ease-out}

  /* ---------- 紧迫感系统（data-urgency：warn 只染数字；critical 红边+呼吸） ---------- */
  .card[data-urgency="warn"] .value{color:var(--urgency-warn)}
  /* 活动卡的「数字」是倒计时 .act-cd：warn 琥珀 / critical 红字 + 呼吸（不放大，保布局） */
  .card[data-urgency="warn"] .act-cd{color:var(--urgency-warn)}
  .card[data-urgency="critical"]{position:relative}
  .card[data-urgency="critical"]::before{content:"";position:absolute;inset:0;
    border-radius:inherit;padding:1px;pointer-events:none;
    background:linear-gradient(135deg, rgba(255,80,80,0.25), transparent 60%);
    -webkit-mask:linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite:xor; mask-composite:exclude}
  .card[data-urgency="critical"] .value{font-size:32px; /* 28px 基准 × 1.15 */
    animation:breathe 3.5s ease-in-out infinite}
  .card[data-urgency="critical"] .progress-glow{box-shadow:0 0 6px 1px rgba(255,90,90,0.7);
    animation:breathe 3.5s ease-in-out infinite}
  .card[data-urgency="critical"] .act-cd{color:var(--urgency-critical);
    animation:breathe 3.5s ease-in-out infinite}
  .card .value,.card .progress-glow{transition:font-size 400ms ease-out, opacity 400ms ease-out}
  .card .act-cd{transition:color 400ms ease-out, opacity 400ms ease-out}
  @keyframes breathe{0%,100%{opacity:0.85}50%{opacity:1}}
  .card-head{display:flex;align-items:center;gap:8px;min-height:50px;
              padding:12px 22px;background:var(--bg-head);
              border-radius:10px 10px 0 0}
  .card-head h2{font-size:14px;font-weight:600;margin:0;display:flex;
                 align-items:center;gap:6px}
  .card-head .spacer{flex:1}
  .card-body{padding:18px 22px}
  /* 标题下方的显眼提示条（灰底小字） */
  .card-tip{padding:8px 16px;font-size:11.5px;color:var(--muted);line-height:1.6;
             background:var(--subtle)}
  /* 账号绑定改版：rem/cred 小徽章作废（状态改由选择卡开关与凭证健康列表表达） */
  /* 复选框区（提醒）与扫码按钮区（凭证）之间的分割线 */
  .v-sep{margin:12px 0 6px}
  .btn-note{margin-top:6px;font-size:11px;color:var(--muted);line-height:1.6}

  /* ---------- 表格：行高 40-44px，表头 12px 灰字 ---------- */
  .tbl{width:100%;border-collapse:collapse;font-size:12.5px}
  .tbl th,.tbl td{padding:10px 12px;
                  text-align:left;white-space:nowrap;vertical-align:middle;line-height:1.5}
  .tbl th{font-size:12px;font-weight:600;color:var(--muted);background:var(--subtle)}
  .tbl tbody tr:nth-child(even){background:var(--zebra)}
  .tbl tbody tr:hover{background:var(--hover)}
  .tbl th.num,.tbl td.num{text-align:center;font-variant-numeric:tabular-nums}
  .tbl td.num b{font-size:13.5px}
  .tbl td.bad{color:var(--err)}
  .tbl td.dim,.dim{color:var(--muted)}
  .tbl td.mono,.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px;
                     font-variant-numeric:tabular-nums}
  .tbl td.wrap{white-space:normal;word-break:break-all}
  /* 每日报表页：下方「最近 7 天」热力图卡拉伸填满视口剩余高度 */
  #tab-history .card:last-child{display:flex;flex-direction:column}
  /* 拉伸容器：内容超高可滚动；底部留 20px 呼吸间距，图例不贴边框 */
  #tab-history .card:last-child > div:not(.card-head){flex:1;min-height:0;
    overflow-y:auto;padding-bottom:20px}
  .gdot{display:inline-block;width:8px;height:8px;border-radius:50%;
        margin-right:8px;vertical-align:1px}
  .pill{display:inline-block;padding:2px 12px;border-radius:999px;
        font-size:11.5px;font-weight:600;line-height:1.7;white-space:nowrap}
  .pill.ok{color:var(--ok);background:var(--ok-bg)}
  .pill.no{color:var(--err);background:var(--err-bg)}
  .err-t{color:var(--err)}
  .soon-t{color:var(--warn);font-weight:600}
  .tgroup td{background:var(--subtle);font-size:11.5px;font-weight:600;color:var(--muted);
             padding:4px 12px;letter-spacing:.03em}
  .mk-ok{display:inline-block;min-width:26px;padding:1px 5px;border-radius:3px;text-align:center;
         color:var(--ok);background:var(--ok-bg);font-weight:700;font-size:13.5px}
  .mk-no{display:inline-block;min-width:26px;padding:1px 5px;border-radius:3px;text-align:center;
         color:var(--err);background:var(--err-bg);font-weight:700;font-size:13.5px}
  .mk-na{color:var(--muted)}
  .bar{display:inline-block;position:relative;width:96px;height:6px;background:var(--line);
       border-radius:999px;vertical-align:middle;margin-right:6px}
  .bar i{display:block;height:100%;background:var(--brand);border-radius:999px}
  .bar i.hot{background:var(--warn)}
  /* 进度条末端发光点（urgent 系统在后续阶段接管配色） */
  .bar .progress-glow,.sbar .progress-glow{position:absolute;top:50%;width:6px;height:6px;
       border-radius:50%;transform:translate(-50%,-50%);background:var(--brand);
       box-shadow:0 0 6px 1px rgba(91,156,245,.65)}
  .bar .progress-glow.hot,.sbar .progress-glow.hot{background:var(--warn);
       box-shadow:0 0 6px 1px rgba(217,164,65,.65)}

  /* ---------- 按钮：细边框、小圆角 ---------- */
  button{font-family:inherit}
  .btn{display:inline-flex;align-items:center;justify-content:center;height:40px;
       padding:0 18px;font-size:13.5px;line-height:1;border-radius:6px;
       border:none;background:var(--btn-bg);
       color:var(--fg);cursor:pointer;white-space:nowrap;font-weight:500}
  .btn:hover{background:var(--btn-hover)}
  .btn.primary{background:var(--brand);color:#fff}
  .btn.primary:hover{background:var(--brand-hover)}
  .btn.sm{height:30px;padding:0 12px;font-size:12px}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .mini{padding:3px 10px;font-size:11.5px;border-radius:4px;border:none;
        background:var(--btn-bg);color:var(--fg);cursor:pointer;white-space:nowrap}
  .mini:hover{background:var(--btn-hover)}
  .mini:disabled{background:var(--line);color:var(--muted);
        opacity:1;cursor:not-allowed}
  .toolbar{display:flex;gap:8px;margin:12px 0}

  /* ---------- 徽章 / 状态点 / (?) 提示 ---------- */
  .badge{font-size:11px;padding:1px 8px;border-radius:3px;background:var(--subtle);
         color:var(--muted);white-space:nowrap}
  .badge.ok{color:var(--ok);background:var(--ok-bg)}
  .badge.warn{color:var(--warn);background:rgba(224,160,64,.15)}
  .badge.err{color:var(--err);background:var(--err-bg)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;flex:none}
  .q{display:inline-grid;place-items:center;width:14px;height:14px;border-radius:50%;
     background:var(--muted);color:#fff;font-size:9.5px;font-weight:700;line-height:1;
     cursor:help;flex:none;user-select:none;font-family:Consolas,monospace;
     vertical-align:-2px;transition:background .15s ease}
  .q::before{content:"?"}
  .q:hover{background:var(--brand)}
  /* 全局 Tooltip 气泡：JS 定位的单个悬浮层，深底白字、瞬间显示 0.2s 淡入 */
  #tip{position:fixed;left:0;top:0;background:#24272c;color:#fff;
       padding:7px 11px;border-radius:4px;
       font-size:11.5px;line-height:1.6;font-weight:400;text-align:left;
       max-width:300px;width:max-content;box-shadow:0 4px 14px rgba(0,0,0,.28);
       opacity:0;visibility:hidden;pointer-events:none;z-index:999;
       transition:opacity .2s ease,visibility .2s ease}
  #tip.on{opacity:1;visibility:visible}
  .opt{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);
       cursor:pointer;white-space:nowrap;user-select:none}
  .opt input{width:14px;height:14px;accent-color:var(--brand);cursor:pointer;margin:0}

  /* ---------- 账号绑定：7:5 分栏容器 ---------- */
  .acct-col{display:flex;flex-direction:column;gap:16px}

  /* ---------- 账号绑定：监控选择卡（Selector Card） ----------
     未选中 = 半透明去色；开关开启（.on）单独覆盖，绝不被滤镜变灰 */
  .sel-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
  .sel-grid .sel-card:last-child:nth-child(odd){grid-column:1/-1}
  .sel-card{position:relative;display:flex;align-items:center;gap:12px;
    padding:12px 14px;border:none;border-radius:8px;
    background:var(--bg-input);cursor:pointer;user-select:none;
    opacity:.6;filter:grayscale(.5);
    transition:opacity .2s ease,filter .2s ease,background .2s ease}
  .sel-card:hover{background:var(--btn-hover)}
  .sel-card.on{opacity:1;filter:none;background:var(--sel)}
  .sel-card input{position:absolute;opacity:0;pointer-events:none}
  .sel-ico{width:34px;height:34px;border-radius:8px;object-fit:cover;flex:none}
  .sel-ico.ph{display:grid;place-items:center;background:var(--subtle);
    color:var(--muted);font-size:14px;font-weight:600}
  .sel-name{flex:1;min-width:0;font-size:13px;font-weight:500;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .switch{flex:none;width:36px;height:20px;border-radius:999px;background:var(--line);
    position:relative;transition:background .2s ease}
  .switch i{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;
    background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.35);transition:left .2s ease}
  .sel-card.on .switch{background:var(--brand)}
  .sel-card.on .switch i{left:18px}

  /* ---------- 账号绑定：扫码胶囊 ---------- */
  .caps-row{display:flex;gap:12px;flex-wrap:wrap}
  .scan-cap{position:relative;display:flex;align-items:center;gap:10px;
    padding:10px 20px;border-radius:999px;border:none;
    background:var(--btn-bg);color:var(--fg);cursor:pointer;font-family:inherit;
    transition:transform .15s ease,box-shadow .15s ease,background .15s ease}
  .scan-cap:hover{background:var(--btn-hover);
    transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.25)}
  .scan-cap:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
  .cap-ico{font-size:18px;line-height:1}
  .cap-txt{display:flex;flex-direction:column;align-items:flex-start;gap:1px}
  .cap-txt b{font-size:13px;font-weight:600}
  .cap-txt small{font-size:10.5px;color:var(--muted)}
  /* 黄点 = 该平台有游戏未绑定凭证 */
  .cap-dot{position:absolute;top:9px;right:13px;width:7px;height:7px;border-radius:50%;
    background:var(--urgency-warn);opacity:0;transition:opacity .2s ease}
  .cap-dot.on{opacity:1}

  /* ---------- 账号绑定：凭证健康紧凑列表（绿/黄/红圆点 + 相对时间） ---------- */
  .cred-list{display:flex;flex-direction:column;padding:6px 18px 10px}
  .cred-row{display:flex;align-items:center;gap:10px;padding:9px 0;
    min-height:44px}
  .cred-row:last-child{border-bottom:none}
  .cred-row .sel-ico{width:28px;height:28px;border-radius:6px}
  .cred-row .sel-ico.ph{font-size:12px}
  .cred-info{flex:1;min-width:0;line-height:1.35}
  .cred-info b{display:block;font-size:12.5px;font-weight:600;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cred-info small{font-size:11px;color:var(--muted)}
  .cred-row.bad .cred-info small{color:var(--err)}
  .cred-dot{flex:none;width:8px;height:8px;border-radius:50%}
  .cred-dot.ok{background:var(--ok)}
  .cred-dot.warn{background:var(--urgency-warn)}
  .cred-dot.err{background:var(--err)}
  .cred-time{flex:none;font-size:11.5px;color:var(--muted);
    font-variant-numeric:tabular-nums;min-width:56px;text-align:right}

  /* ---------- 表单 ---------- */
  .field{margin:2px 0}
  .field input{width:100%;height:42px;padding:0 16px;border-radius:6px;
               border:none;background:var(--bg-input);color:var(--fg);
               font-size:14px;font-family:ui-monospace,Consolas,"Courier New",monospace}
  .field textarea{width:100%;padding:10px 14px;border-radius:6px;
               border:none;background:var(--bg-input);color:var(--fg);
               font:12.5px/1.55 ui-monospace,Consolas,"Courier New",monospace;
                  resize:vertical;min-height:112px}
  .field input:focus,.field textarea:focus{outline:none;
               box-shadow:0 0 0 2px var(--focus-ring)}
  input::placeholder,textarea::placeholder{color:var(--muted);opacity:.6}
  .hint{margin:0;padding:6px 12px;font-size:11.5px;color:var(--muted);line-height:1.6}
  .card-body .hint{padding:6px 0 0}
  .hint:empty{display:none}
  .hint b{color:var(--fg)}
  .hint code{background:var(--subtle);padding:0 4px;border-radius:2px;font-size:11px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
  @media (max-width:520px){ .row{grid-template-columns:1fr} }
  .row3{display:grid;grid-template-columns:1fr auto;gap:10px;margin-top:10px;
        align-items:stretch}
  .row3 .btn,.row3 .mini{border-radius:4px}
  input[type=time],input[type=number]{width:100%;height:42px;padding:0 14px;
                   border-radius:6px;border:none;
                   background:var(--bg-input);color:var(--fg);
                   font-size:14px;font-family:ui-monospace,Consolas,monospace}
  input[type=time]:focus,input[type=number]:focus{outline:none;
                   box-shadow:0 0 0 2px var(--focus-ring)}

  /* ---------- 提醒时间点 chips ---------- */
  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{display:inline-flex;align-items:center;gap:5px;background:var(--subtle);
        border-radius:3px;padding:2px 3px 2px 9px;
        font-size:12.5px;font-family:ui-monospace,Consolas,monospace}
  .chip button{width:18px;height:18px;padding:0;border-radius:2px;background:transparent;
               color:var(--muted);border:none;font-size:14px;line-height:1;cursor:pointer}
  .chip button:hover{background:var(--err);color:#fff}
  .chip.empty{color:var(--muted);background:transparent;padding:2px 9px;font-family:inherit}

  /* ---------- Win11 设置行（定时与状态）：左 240px 说明 · 中留白 · 右控件 ---------- */
  .set-card > .card-body{padding:0}
  .set-head{display:flex;align-items:center;gap:16px;padding:13px 22px;min-height:52px;
    cursor:pointer;user-select:none;list-style:none;
    -webkit-tap-highlight-color:transparent}
  .set-head::-webkit-details-marker{display:none}
  .set-head:hover{background:var(--hover)}
  .set-lead{width:240px;flex:none;display:flex;gap:10px;align-items:center;min-width:0}
  .set-ico{font-size:17px;width:22px;text-align:center;flex:none;font-style:normal}
  .set-txt{min-width:0}
  .set-txt b{display:block;font-size:13px;font-weight:600;line-height:1.35;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .set-txt small{display:block;font-size:11px;color:var(--muted);line-height:1.5;
                 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .set-end{margin-left:auto;display:flex;align-items:center;gap:12px;flex:none}
  .set-note{font-size:11px;color:var(--muted);white-space:nowrap}
  /* 展开箭头：贴最右，展开时翻转 */
  .set-head::after{content:"";width:0;height:0;flex:none;margin-left:2px;
    border-left:4.5px solid transparent;border-right:4.5px solid transparent;
    border-top:5.5px solid var(--muted);transition:transform .15s ease}
  .set-item[open] > .set-head::after{transform:rotate(180deg)}
  .set-body{padding:12px 22px 16px 272px}
  .set-body .hint{font-size:11px;padding:8px 0 0}
  .set-item.is-off .set-body{opacity:.55}
  .set-item.is-off .set-body:focus-within{opacity:1}
  .set-item.is-off .set-txt b{color:var(--muted)}
  @media (max-width:760px){
    .set-lead{width:100%}
    .set-end{margin-left:32px}
    .set-body{padding-left:22px}
  }

  /* ---------- 24 小时标尺（提醒时间点打点） ---------- */
  .ruler{position:relative;height:60px;margin:4px 12px 12px;min-width:0}
  .ruler-track{position:absolute;left:0;right:0;bottom:20px;height:4px;
               background:var(--line);border-radius:2px}
  .ruler-tick{position:absolute;bottom:0;transform:translateX(-50%);
              font-size:10.5px;color:var(--muted);
              font-family:ui-monospace,Consolas,monospace}
  .ruler-tick::before{content:"";position:absolute;left:50%;bottom:100%;width:1px;
                      height:5px;background:var(--line)}
  .ruler-dot{position:absolute;bottom:15px;transform:translateX(-50%);cursor:default}
  .ruler-dot::before{content:"";display:block;width:10px;height:10px;border-radius:50%;
                     background:var(--brand);border:2px solid var(--surface);
                     box-shadow:0 0 0 1.5px var(--brand)}
  .ruler-dot span{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
                  margin-bottom:2px;font-size:11px;font-weight:700;color:var(--brand);
                  font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
  .ruler-empty{position:absolute;left:0;right:0;bottom:22px;text-align:center;
               font-size:11.5px;color:var(--muted)}

  /* ---------- KPI 摘要条（吸顶锚点：80px 常态 / 56px compact 收缩） ---------- */
  .kpi-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;
           flex:none;margin-bottom:16px;
           position:sticky;top:0;z-index:50;min-height:80px;background:var(--bg-base);
           transition:min-height 250ms ease-out, padding 250ms ease-out}
  .kpi-bar.compact{min-height:56px;grid-template-columns:repeat(2,1fr)}
  .kpi-bar.compact .kpi{display:none}
  .kpi-bar.compact .kpi.keep{display:block}
  .kpi-bar.compact .kpi-value{font-size:24px}
  .kpi-bar.compact .kpi-sub{display:none}
  .kpi{background:var(--bg-elevated);border:none;border-radius:8px;
       padding:12px 18px;min-width:0}
  .kpi-label{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:6px}
  .kpi-value,.value{font-size:28px;font-weight:700;line-height:1.3;margin-top:2px;
             font-variant-numeric:tabular-nums;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis}
  .kpi-value.ok,.value.ok{color:var(--ok)}
  .kpi-value.warn,.value.warn{color:var(--warn)}
  .kpi-value.err,.value.err{color:var(--err)}
  .kpi-sub{font-size:11px;color:var(--muted);margin-top:1px;
           white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  @media (max-width:1000px){ .kpi-bar{grid-template-columns:repeat(2,1fr)} }
  @media (max-width:520px){ .kpi-bar{grid-template-columns:1fr} }

  /* ---------- 底部日志控制台：固定底栏（不随内容滚动、不悬浮遮挡） ----------
     收缩 48px（只有把手）/ 展开 200px（把手 48 + 日志体 152） */
  .console{flex-shrink:0;background:var(--surface);
           border-radius:0;overflow:hidden}
  .console-head{display:flex;align-items:center;gap:12px;padding:0 18px;
                height:48px;min-height:48px;
                cursor:pointer;user-select:none;list-style:none}
  .console-head::-webkit-details-marker{display:none}
  .console-head:hover{background:var(--hover)}
  .console-title{font-size:13px;font-weight:600;white-space:nowrap}
  .console-last{flex:1;min-width:0;font-family:ui-monospace,Consolas,monospace;
                font-size:11.5px;color:var(--muted);white-space:nowrap;
                overflow:hidden;text-overflow:ellipsis;text-align:right}
  .console-body{background:var(--bg-sub);
                font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:11.5px;
                line-height:1.8;padding:10px 16px;height:152px;overflow-y:auto;
                font-variant-numeric:tabular-nums}
  .console-body .c-time{color:var(--muted)}
  .console-body .cline.c-warn{color:var(--warn)}
  .console-body .cline.c-err{color:var(--err)}
  .console-body .cline.c-ok{color:var(--ok)}
  .console-empty{color:var(--muted)}

  /* ---------- 辅助卡片：巡检动态 / 推送预览 / 活动空状态 ---------- */
  .log-row{display:flex;align-items:center;gap:10px;padding:9px 0;
           font-size:12.5px;flex-wrap:wrap}
  .log-date{font-weight:600;white-space:nowrap}
  .log-time{color:var(--muted);font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .log-detail{color:var(--muted);font-size:11.5px}
  .act-empty{display:flex;flex-direction:column;align-items:center;gap:8px;
             padding:36px 16px;color:var(--muted);font-size:12.5px}
  .act-empty-icon{font-size:30px;line-height:1}
  .phone-mock{background:#1b1e23;border-radius:10px;
              padding:14px}
  .pv-bubble{background:#2b3037;color:#e8eaed;border-radius:8px;padding:10px 12px;
             max-width:440px}
  .pv-app{font-size:10.5px;color:#9aa0a6;margin-bottom:4px;display:flex;
          align-items:center;gap:6px}
  .pv-app i{width:8px;height:8px;border-radius:2px;background:var(--brand);
            display:inline-block}
  .pv-title{font-size:12.5px;font-weight:600;color:#fff;margin-bottom:3px}
  .pv-body{font-size:11.5px;line-height:1.7;color:#c6cbd2;white-space:pre-line}

  /* ---------- 开关（BetterGI 尺寸） ---------- */
  .toggle{position:relative;display:inline-block;width:40px;height:22px;flex:none;vertical-align:middle}
  .toggle input{position:absolute;opacity:0;width:0;height:0;margin:0}
  .toggle i{position:absolute;inset:0;border-radius:999px;background:var(--line);
    transition:background .15s ease;pointer-events:none}
  .toggle i::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;
    border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);
    transition:transform .15s ease}
  .toggle input:checked + i{background:var(--brand)}
  .toggle input:checked + i::after{transform:translateX(18px)}
  .toggle input:focus-visible + i{box-shadow:0 0 0 2px rgba(11,103,194,.3)}
  .toggle input:disabled + i{opacity:.45}

  /* ---------- 守护进程信息 ---------- */
  .kv-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 24px;font-size:12.5px}
  @media (max-width:520px){ .kv-grid{grid-template-columns:1fr} }
  .kv-grid > div{display:flex;justify-content:space-between;gap:10px;
    padding:6px 0}
  .kv-grid span{color:var(--muted);white-space:nowrap}
  .kv-grid b{font-weight:500;text-align:right;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;font-family:ui-monospace,Consolas,monospace;font-size:12px}

  /* ---------- 体力阈值 ---------- */
  .resin-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:10px}
  @media (max-width:760px){ .resin-row{grid-template-columns:repeat(2,1fr)} }
  .resin-row label{display:block;font-size:11.5px;color:var(--muted)}
  .resin-row input[type=number]{margin-top:3px}

  /* ---------- 通知 / 警告条 ---------- */
  .notice{margin-bottom:12px;padding:8px 12px;border-radius:6px;
          font-size:12.5px;line-height:1.7;background:rgba(217,119,6,.07)}
  .notice b{color:var(--warn)}
  .notice.info{background:var(--subtle)}
  .notice.info b{color:var(--fg)}
  .notice a{color:var(--brand);text-decoration:none;font-weight:500;
            border-bottom:1px solid currentColor;cursor:pointer}

  /* ---------- 扫码登录区 ---------- */
  .stage{padding:18px;text-align:center}
  .qrbox{display:inline-flex;padding:10px;background:#fff;
         border-radius:8px;min-height:224px;min-width:224px;align-items:center;
         justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.2)}
  .qrbox img{width:224px;height:224px;display:block;image-rendering:pixelated}
  .qrbox .ph{color:var(--muted);font-size:12.5px;padding:0 20px}
  .status{margin-top:12px;display:flex;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap}
  #msg{margin:0;font-size:13px}
  .kv{margin-top:12px;font-size:12.5px;color:var(--muted);text-align:left;
      padding-top:10px}
  .kv code{background:var(--subtle);padding:0 5px;border-radius:2px;font-size:11.5px}
  .res{display:flex;flex-direction:column;gap:3px;margin-top:6px;text-align:left}
  .res div{display:flex;align-items:center;gap:6px}
  .res .i-ok{color:var(--ok)}.res .i-no{color:var(--err)}

  /* ---------- 扫码浮层（modal） ---------- */
  .modal-mask{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.55);
    display:flex;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
  .modal-mask[hidden]{display:none}
  .modal{background:var(--bg-card);border:none;border-radius:10px;
    padding:18px 22px 20px;max-width:min(420px,92vw);text-align:center;
    box-shadow:0 16px 48px rgba(0,0,0,.5)}
  .modal-head{display:flex;align-items:center;justify-content:space-between;gap:20px;
    margin-bottom:14px}
  .modal-head b{font-size:14px;font-weight:600}
  .modal-x{width:26px;height:26px;border-radius:5px;border:none;
    background:var(--btn-bg);color:var(--muted);cursor:pointer;font-size:12px;line-height:1}
  .modal-x:hover{background:var(--btn-hover);color:var(--fg)}

  #test-result{margin-top:4px;padding:8px 12px;font-size:12.5px}
  .spin{display:inline-block;width:11px;height:11px;border:2px solid var(--line);
        border-top-color:var(--brand);border-radius:50%;animation:s .8s linear infinite;
        vertical-align:-1px;margin-right:4px}
  @keyframes s{to{transform:rotate(360deg)}}

  /* ---------- 设备伪装：details 折叠（默认收起）+ 展开后 45/55 分栏 ---------- */
  #dev-card summary{list-style:none;display:flex;align-items:center;gap:8px;
    min-height:50px;padding:12px 22px;cursor:pointer;user-select:none}
  #dev-card summary::-webkit-details-marker{display:none}
  #dev-card summary h2{font-size:14px;font-weight:600;margin:0;
    display:flex;align-items:center;gap:6px}
  #dev-card[open] summary{background:var(--bg-head)}
  .dev-arrow{flex:none;width:14px;height:14px;color:var(--muted);
    transition:transform .25s ease}
  #dev-card[open] .dev-arrow{transform:rotate(90deg)}
  .dev-body{padding:18px 22px}
  .dev-grid{display:grid;grid-template-columns:45% 55%;gap:18px;align-items:start}
  @media (max-width:760px){ .dev-grid{grid-template-columns:1fr} }
  /* 步骤 1-2-3：dashed 竖线做连线，数字圆珠压在线上 */
  .dsteps{position:relative;margin:0 0 14px 9px;padding-left:20px;
    border-left:2px dashed var(--border-strong);
    display:flex;flex-direction:column;gap:12px}
  .dstep{position:relative;display:flex;gap:8px;align-items:flex-start;
    font-size:12.5px;color:var(--muted);line-height:1.5}
  .dstep b{position:absolute;left:-30px;top:0;width:18px;height:18px;border-radius:50%;
    background:var(--btn-bg);color:var(--muted);
    font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center}
  .apk{text-align:center}
  .apk-qr{display:inline-block;padding:6px;background:#fff;
          border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
  .apk-qr img{width:126px;height:126px;display:block}
  .apk-meta{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.5;word-break:break-all}
  /* 代码粘贴框：等宽字体 + 内阴影质感 */
  .dev-text{width:100%;min-height:132px;padding:10px 14px;border-radius:6px;
    border:none;background:var(--bg-input);color:var(--fg);
    font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px;
    line-height:1.6;resize:vertical;
    box-shadow:inset 0 2px 6px rgba(0,0,0,.18)}
  .dev-text:focus{outline:none;
    box-shadow:inset 0 2px 6px rgba(0,0,0,.18),0 0 0 2px var(--focus-ring)}

  /* ---------- 活动日历卡片流（auto-fill 等高卡片流，所有卡片一律等大） ---------- */
  .act-grid{display:grid;
            grid-template-columns:repeat(auto-fill, minmax(min(320px, 100%), 1fr));
            gap:16px;grid-auto-flow:dense;grid-auto-rows:1fr}
  .act-grid > *{min-width:0}
  /* 卡片底色比页面卡面再提亮一档，靠色阶分层，不用描边 */
  .acard{display:flex;flex-direction:column;gap:8px;
         min-height:120px;justify-content:space-between;
         padding:14px 16px;font-size:12.5px;
         background:var(--bg-raise)}
  /* 头部（图标+游戏名）独立色块 + 底边框，与时间/正文分层 */
  .acard-top{display:flex;align-items:center;gap:8px;min-width:0;
    background:var(--bg-head);margin:-14px -16px 0;padding:10px 16px 8px;
    border-radius:10px 10px 0 0}
  .acard-top .act-icon{flex:none}
  /* 抽屉里的已完成卡更矮更扁 */
  .act-drawer .acard{min-height:80px}
  /* 等高纪律：grid-auto-rows:1fr 让所有行同高；卡内 space-between 让标题/时间/进度各占其位 */
  @media (max-width:640px){ .act-grid > *{grid-column:1/-1} }
  .acard-top .act-game{flex:1;min-width:0;font-weight:600;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .acard-title{font-size:1.15em;font-weight:600;line-height:1.5;margin:0;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
    -webkit-box-orient:vertical;white-space:normal}
  /* 卡片头部精确到分的时间：未开始蓝 / 进行中次级灰 / 已结束三级灰 */
  .act-time{font-size:1em;font-variant-numeric:tabular-nums;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .act-time.t-up{color:var(--accent-blue)}
  .act-time.t-on{color:var(--text-secondary)}
  .act-time.t-end{color:var(--text-tertiary)}
  /* 倒计时是卡片的核心信息：加粗放大（即将结束另有 --urgency-warn 高亮） */
  .act-cd{font-size:1.25em;font-weight:700;color:var(--fg);
          font-variant-numeric:tabular-nums}
  .act-cd.soon{color:var(--urgency-warn);font-weight:600}
  .act-cd.ended{color:var(--text-tertiary)}
  .act-prog{display:flex;align-items:center;gap:8px;font-size:.95em}
  .act-prog .sbar{max-width:220px;height:6px;border-radius:999px}
  .act-prog .sbar i{border-radius:999px}
  /* 底部「已完成」抽屉：48px 把手 + 40vh 展开体，内部卡片半透明无进度条 */
  .act-drawer{margin-top:16px;border:none;border-radius:8px;
              background:var(--bg-card);overflow:hidden}
  .act-drawer-handle{display:flex;align-items:center;gap:8px;width:100%;height:48px;
    padding:0 18px;background:transparent;border:none;color:var(--text-secondary);
    font-size:12.5px;font-weight:600;cursor:pointer;text-align:left}
  .act-drawer-handle:hover{background:var(--hover)}
  .act-drawer-handle::after{content:"";margin-left:auto;flex:none;
    border-left:4.5px solid transparent;border-right:4.5px solid transparent;
    border-top:5.5px solid var(--text-tertiary);transition:transform .2s ease}
  .act-drawer.open .act-drawer-handle::after{transform:rotate(180deg)}
  .act-drawer-body{max-height:0;overflow:hidden;transition:max-height .3s ease}
  .act-drawer.open .act-drawer-body{max-height:40vh;overflow-y:auto;padding:2px 12px 12px}
  .act-drawer .act-grid{grid-auto-rows:1fr}
  .act-drawer .acard{opacity:.5}
  .act-drawer .acard .act-prog{display:none}
  .acard .act-del{flex:none}
  .acard:hover .act-del{opacity:1}
  .act-del{width:22px;height:22px;padding:0;border-radius:3px;border:1px solid transparent;
    background:transparent;color:var(--muted);font-size:14px;line-height:1;cursor:pointer;
    opacity:.5}
  .act-del:hover{background:rgba(220,38,38,.1);color:var(--err);border-color:rgba(220,38,38,.3)}
  .act-restore{font-size:12px;color:var(--brand);cursor:pointer;text-decoration:none}
  .act-restore:hover{text-decoration:underline}
  .act-unsupported{padding:6px 12px;font-size:11.5px;color:var(--muted)}
  .act-unsupported:empty{display:none}
  .au-chip{display:inline-block;font-size:11.5px;color:var(--muted);background:var(--subtle);
    border-radius:3px;padding:1px 8px;margin:0 6px 4px 0}
  .au-title{margin-bottom:4px}

  /* ---------- 游戏迷你卡（每日报表概览） ---------- */
  .gcards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  @media (max-width:1500px){ .gcards{grid-template-columns:repeat(2,1fr)} }
  @media (max-width:860px){ .gcards{grid-template-columns:1fr} }
  .gcard{border-radius:8px;padding:14px 16px;min-width:0;
         display:flex;flex-direction:column;gap:10px}
  .gcard-head{display:flex;align-items:center;gap:10px;min-width:0}
  .gcard-ico{width:40px;height:40px;border-radius:8px;object-fit:cover;flex:none}
  .gcard-ico.ph{display:grid;place-items:center;background:var(--subtle);
    color:var(--muted);font-size:16px}
  .gcard-name{flex:1;min-width:0}
  .gcard-name b{display:block;font-size:14px;line-height:1.35;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .gcard-name small{color:var(--muted);font-size:11px}
  .gcard-streak{text-align:right;flex:none}
  .gcard-streak b{display:block;font-size:24px;line-height:1.15;
    font-variant-numeric:tabular-nums}
  .gcard-streak small{color:var(--muted);font-size:10.5px}
  .gcard-today{font-size:12.5px}
  /* 进度透明化：每条判定任务的具体进度；未完成项数字用警示色 */
  .gcard-tasks{display:flex;flex-wrap:wrap;gap:4px 0;font-size:11.5px;color:var(--muted);min-width:0}
  .gcard-tasks .tp-item{white-space:nowrap}
  .gcard-tasks .tp-item b{font-weight:600;color:var(--fg);font-variant-numeric:tabular-nums}
  .gcard-tasks .tp-item.tp-miss b{color:var(--urgency-warn)}
  .gcard-tasks .tp-sep{font-style:normal;margin:0 6px;color:var(--text-tertiary)}
  .gcard-stam{display:flex;align-items:center;gap:8px;font-size:11.5px;min-width:0}
  .gcard-stam .dim{white-space:nowrap;flex:none}
  .sbar{flex:1;position:relative;height:6px;background:var(--line);border-radius:999px;min-width:0}
  .sbar i{display:block;height:100%;background:var(--brand);border-radius:999px}
  .sbar i.hot{background:var(--warn)}
  .gcard-foot{font-size:11.5px;color:var(--muted);
              padding-top:8px;margin-top:auto}
  .gcard-foot b{color:var(--fg)}

  /* ---------- 最近 7 天热力图（GitHub 式） ---------- */
  .heatmap{display:flex;flex-direction:column;gap:6px;width:100%;
           background:var(--bg-sub);border-radius:6px;padding:10px}
  .hm-row{display:grid;grid-template-columns:150px repeat(7,1fr);gap:6px;min-width:0}
  @media (max-width:760px){ .hm-row{grid-template-columns:96px repeat(7,1fr)} }
  .hm-label{display:flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;
    min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .hm-cell{min-height:38px;border-radius:4px;display:flex;align-items:center;
    justify-content:center;font-size:12px;font-weight:600;min-width:0}
  .hm-head .hm-cell{background:transparent;min-height:22px;font-weight:400;
    font-size:11px;color:var(--muted)}
  .hm-head .hm-cell.today{color:var(--brand);font-weight:700}
  .hm-row:not(.hm-head) .hm-cell.today{box-shadow:inset 0 0 0 2px var(--brand)}
  .hm-cell.ok{background:var(--ok);color:#fff}
  .hm-cell.err{background:var(--err);color:#fff}
  .hm-cell.na{background:var(--subtle);color:var(--muted)}
  .hm-cell.na.wait{background:repeating-linear-gradient(135deg,
    var(--subtle) 0 4px, var(--hover) 4px 8px)}
  .hm-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);
    margin-top:8px;flex-wrap:wrap}
  .hm-legend .hm-cell{min-height:18px;min-width:20px;padding:0 5px;font-size:10.5px}

  /* ---------- 通知绑定三区（邮箱客户端式） ---------- */
  .stack{display:flex;flex-direction:column;gap:16px;min-width:0}
  .ch-list{display:flex;flex-direction:column;gap:8px}
  .ch-item{display:flex;align-items:center;gap:10px;padding:11px 12px;
    border:none;border-radius:6px;cursor:pointer;user-select:none;min-width:0;
    background:var(--subtle)}
  .ch-item:hover{background:var(--hover)}
  .ch-item.on{background:var(--sel)}
  .ch-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
  .ch-dot.bad{background:var(--warn)}
  .ch-item > div{flex:1;min-width:0}
  .ch-item b{display:block;font-size:13px;line-height:1.4}
  .ch-item small{color:var(--muted);font-size:11px}
  .ch-state{font-size:10.5px;color:var(--muted);text-align:right;flex:none;max-width:92px;
    overflow:hidden;text-overflow:ellipsis;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;white-space:normal}
  /* 编辑区输入框固定宽度，不拉满 */
  #ed-serverchan .field,#ed-qmsg .field{max-width:420px}
  .recent{display:flex;flex-direction:column;font-size:11px;color:var(--muted);min-width:0}
  .recent div{padding:6px 0;
    font-family:ui-monospace,Consolas,monospace;word-break:break-all}

  /* ---------- 空状态 / Toast ---------- */
  .emptybox{padding:20px 12px;text-align:center;color:var(--muted);font-size:12.5px;line-height:1.8}
  #toast{position:fixed;left:50%;top:18px;transform:translate(-50%,-14px);
         background:var(--fg);color:var(--bg);padding:6px 16px;border-radius:2px;
         font-size:12.5px;opacity:0;pointer-events:none;z-index:99;
         transition:opacity .2s,transform .2s;box-shadow:0 2px 10px rgba(0,0,0,.25);
         max-width:88vw;text-align:center}
  #toast.show{opacity:1;transform:translate(-50%,0)}
  #toast.err{background:var(--err);color:#fff}
</style>
</head>
<body>
<div class="app">
  <div class="app-body">
  <aside class="side">
    <div class="brand">
      <div class="logo">R</div>
      <div><b>ResinLoop</b><span>日常巡查</span></div>
    </div>
    <nav class="nav">
      <button type="button" data-tab="history" class="on"><span class="nav-ico">📊</span>每日报表</button>
      <button type="button" data-tab="query"><span class="nav-ico">🔍</span>信息查询</button>
      <button type="button" data-tab="account"><span class="nav-ico">🔗</span>账号绑定</button>
      <button type="button" data-tab="notify"><span class="nav-ico">📮</span>通知绑定</button>
      <button type="button" data-tab="schedule"><span class="nav-ico">⏰</span>定时与状态</button>
    </nav>
    <div class="side-foot">
      <button type="button" class="theme-btn" id="theme-btn" title="切换深浅色主题">主题：跟随系统</button>
      <div style="margin-top:5px">v1.1.8 · 本地运行</div>
    </div>
  </aside>

  <main class="main">

  <!-- ======================= KPI 摘要条（全局视觉锚点） ======================= -->
  <div class="kpi-bar" id="kpi-bar">
    <div class="kpi card">
      <div class="kpi-label">今日完成
        <span class="q" data-tip="按今天最近一次巡检结果统计；查询失败不计为未完成"></span></div>
      <div class="kpi-value value" id="k-today">–</div>
      <div class="kpi-sub" id="k-today-sub">等待巡检数据</div>
    </div>
    <div class="kpi card">
      <div class="kpi-label">下次巡查</div>
      <div class="kpi-value value" id="k-next">–</div>
      <div class="kpi-sub" id="k-next-sub">读取中…</div>
    </div>
    <div class="kpi card">
      <div class="kpi-label">凭证健康
        <span class="q" data-tip="已绑定登录凭证的账号数 / 启用账号总数"></span></div>
      <div class="kpi-value value" id="k-cred">–</div>
      <div class="kpi-sub" id="k-cred-sub">读取中…</div>
    </div>
    <div class="kpi card">
      <div class="kpi-label">体力临满
        <span class="q" data-tip="体力达到 85% 以上的游戏数；在「信息查询」里查询后更新"></span></div>
      <div class="kpi-value value" id="k-resin">–</div>
      <div class="kpi-sub" id="k-resin-sub">查询后显示</div>
    </div>
  </div>

  <!-- ======================= 每日报表 ======================= -->
  <section class="tabpanel on" id="tab-history">
    <section class="card s12">
      <div class="card-head">
        <h2>📊 游戏概览</h2>
        <span class="q" data-tip="每款游戏一张卡：今日完成状态、当前体力与连续完成天数"></span>
        <span class="badge" id="h-month">-</span>
        <span class="spacer"></span>
        <button type="button" class="btn sm" id="btn-history-refresh">刷新</button>
      </div>
      <div id="h-stats"></div>
    </section>

    <section class="card s12">
      <div class="card-head">
        <h2>📅 最近 7 天</h2>
        <span class="q" data-tip="绿色 = 当天完成，红色 = 查过但未完成，灰 = 当天未运行或未查到"></span>
      </div>
      <div id="h-table"></div>
    </section>
  </section>

  <!-- ======================= 信息查询 ======================= -->
  <section class="tabpanel" id="tab-query">
    <section class="card s12">
      <div class="card-head">
        <h2>⚡ 实时体力</h2>
        <span class="q" data-tip="手动查询所有启用账号，不写入历史、不推送"></span>
        <span class="spacer"></span>
        <button type="button" class="btn sm" id="btn-live-refresh">刷新</button>
      </div>
      <div id="live-grid"><div class="emptybox">读取中…</div></div>
      <div class="hint" id="live-hint"></div>
    </section>

    <section class="card s12">
      <div class="card-head">
        <h2>🗓️ 活动日历</h2>
        <span class="q" data-tip="只保留给抽卡资源的活动（原石/星琼/菲林/合成玉·源石）；剩余 ≤72 小时时在推送末尾提醒"></span>
        <span class="spacer"></span>
        <button type="button" class="btn sm" id="btn-act-refresh">刷新</button>
      </div>
      <div id="act-list"><div class="emptybox">读取中…</div></div>
      <div class="hint" id="act-hint"></div>
      <div class="act-unsupported" id="act-unsupported"></div>
    </section>
  </section>

  <!-- ======================= 账号绑定（7:5 分栏 · 渐进式披露） ======================= -->
  <section class="tabpanel" id="tab-account">
    <div class="notice s12" id="notice" style="display:none"></div>

    <div class="acct-col s7">
      <!-- 扫码胶囊 -->
      <section class="card">
        <div class="card-head">
          <h2>🔑 扫码登录</h2>
          <span class="q" data-tip="点击胶囊开始扫码，更新所选游戏的登录凭证；同一平台的多个游戏可能绑定在不同账号上。黄点 = 该平台有游戏未绑定凭证"></span>
        </div>
        <div class="card-body">
          <div class="caps-row">
            <button type="button" class="scan-cap" id="cap-m" onclick="startMihoyo()">
              <span class="cap-dot" id="cap-dot-m"></span>
              <span class="cap-ico">⭐</span>
              <span class="cap-txt"><b>米游社</b><small>更新所选游戏凭证</small></span>
            </button>
            <button type="button" class="scan-cap" id="cap-s" onclick="startSkland()">
              <span class="cap-dot" id="cap-dot-s"></span>
              <span class="cap-ico">🏝️</span>
              <span class="cap-txt"><b>森空岛</b><small>更新所选游戏凭证</small></span>
            </button>
          </div>
          <div class="btn-note">扫码只更新登录凭证；是否参与每日巡检由下方监控开关决定。</div>
        </div>
      </section>

      <!-- 游戏监控：Selector Card 网格 -->
      <section class="card">
        <div class="card-head">
          <h2>🎮 游戏监控
            <span class="q" data-tip="开启的游戏参与每日巡检并推送通知；关闭则不再巡检、不再推送。点击卡片任意位置即可切换"></span>
          </h2>
          <span class="spacer"></span>
          <button type="button" class="btn sm" id="btn-selall" onclick="toggleAllCards()">全选</button>
        </div>
        <div class="card-body">
          <div class="sel-grid">
            <label class="sel-card on">
              <input type="checkbox" name="m" value="genshin" checked>
              <span class="sel-ico ph">原</span>
              <span class="sel-name">原神</span>
              <span class="switch"><i></i></span>
            </label>
            <label class="sel-card on">
              <input type="checkbox" name="m" value="starrail" checked>
              <span class="sel-ico ph">崩</span>
              <span class="sel-name">崩坏：星穹铁道</span>
              <span class="switch"><i></i></span>
            </label>
            <label class="sel-card on">
              <input type="checkbox" name="m" value="zzz" checked>
              <span class="sel-ico ph">绝</span>
              <span class="sel-name">绝区零</span>
              <span class="switch"><i></i></span>
            </label>
            <label class="sel-card on">
              <input type="checkbox" name="s" value="arknights" checked>
              <span class="sel-ico ph">舟</span>
              <span class="sel-name">明日方舟</span>
              <span class="switch"><i></i></span>
            </label>
            <label class="sel-card on">
              <input type="checkbox" name="s" value="endfield" checked>
              <span class="sel-ico ph">终</span>
              <span class="sel-name">明日方舟：终末地</span>
              <span class="switch"><i></i></span>
            </label>
          </div>
        </div>
      </section>
    </div>

    <div class="acct-col s5">
      <!-- 凭证健康：紧凑列表 -->
      <section class="card">
        <div class="card-head">
          <h2>🔐 凭证健康
            <span class="q" data-tip="绿点 = 已绑定 · 黄点 = 早期绑定（无更新记录）· 红点 = 未绑定；时间为相对时间，更早绑定的账号没有时间记录，下次扫码后会开始记录"></span>
          </h2>
          <span class="spacer"></span>
          <span class="badge" id="cred-health-badge">-</span>
        </div>
        <div id="cred-health"><div class="emptybox">读取中…</div></div>
      </section>

      <!-- 设备伪装：details 折叠，默认收起 -->
      <details class="card" id="dev-card">
        <summary>
          <h2>📱 设备伪装
            <span class="q" data-tip="米游社按设备指纹判断风险；用手机真实档案通过率最高，不填则用内置通用机型"></span>
          </h2>
          <span class="spacer"></span>
          <span class="badge" id="dev-badge">读取中</span>
          <svg class="dev-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5l4.5 4.5L6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </summary>
        <div class="dev-body">
          <div class="dev-grid">
            <div class="dev-left">
              <div class="dsteps">
                <div class="dstep"><b>1</b><span>手机扫码下载并安装「设备信息」App</span></div>
                <div class="dstep"><b>2</b><span>打开 App，点「复制设备信息」</span></div>
                <div class="dstep"><b>3</b><span>把复制到的内容粘到右边，点保存</span></div>
              </div>
              <div class="apk" id="apk-box">
                <div class="apk-qr" id="apk-qr"><span class="ph" style="font-size:11px">二维码生成中…</span></div>
                <div class="apk-meta" id="apk-meta"></div>
              </div>
            </div>
            <div class="dev-right">
              <textarea id="dev-text" class="dev-text" spellcheck="false"
                placeholder='把「设备信息」App 复制出来的内容粘贴到这里，形如：
{"deviceModel":"24122RKC7C","androidVersion":"16","deviceFingerprint":"...","oaid":"..."}'></textarea>
              <div class="toolbar">
                <button id="btn-dev-save" class="btn primary"
                        title="保存后需重新扫码，否则登录设备与查询设备不一致">保存并清空指纹缓存</button>
                <button id="btn-dev-reset" class="btn">恢复默认</button>
              </div>
              <div class="hint" id="dev-current">正在读取当前设备档案…</div>
            </div>
          </div>
        </div>
      </details>
    </div>

    <!-- 扫码浮层（modal） -->
    <div class="modal-mask" id="stage" hidden>
      <div class="modal">
        <div class="modal-head">
          <b id="modal-title">扫码登录</b>
          <button type="button" class="modal-x" onclick="reset()" title="关闭">✕</button>
        </div>
        <div class="qrbox" id="qrbox"><span class="ph">二维码准备中…</span></div>
        <div class="status">
          <span class="badge" id="badge">准备中</span>
          <p id="msg"></p>
        </div>
        <div class="kv" id="detail" hidden></div>
        <div class="toolbar" style="justify-content:center">
          <button class="btn" onclick="reset()">关闭</button>
        </div>
      </div>
    </div>
  </section>

  <!-- ======================= 通知绑定（三区：列表 / 编辑 / 预览） ======================= -->
  <section class="tabpanel" id="tab-notify">
    <!-- 左：渠道列表 -->
    <section class="card s3">
      <div class="card-head"><h2>📮 渠道</h2></div>
      <div class="card-body">
        <div class="ch-list">
          <div class="ch-item on" data-ch="serverchan" id="ch-item-serverchan">
            <span class="ch-dot" id="ch-dot-serverchan"></span>
            <div><b>Server酱</b><small>推送到微信</small></div>
            <span class="ch-state" id="ch-state-serverchan">读取中…</span>
          </div>
          <div class="ch-item" data-ch="qmsg" id="ch-item-qmsg">
            <span class="ch-dot" id="ch-dot-qmsg"></span>
            <div><b>Qmsg 酱</b><small>推送到 QQ</small></div>
            <span class="ch-state" id="ch-state-qmsg">读取中…</span>
          </div>
        </div>
        <div class="v-sep"></div>
        <label class="opt" style="justify-content:space-between;width:100%">
          <span>全部完成也推送</span>
          <input type="checkbox" id="always-notify">
        </label>
        <div class="hint">默认只在有未完成项时推送；勾选后全部做完也发一条。</div>
      </div>
    </section>

    <!-- 中：当前渠道编辑区 -->
    <section class="card s5">
      <div class="card-head">
        <h2 id="ed-title">💬 Server酱（微信）
          <span class="q" data-tip="Key 由后端打码显示：不修改输入框 = 保持原值；清空后保存 = 删除该 Key"></span>
        </h2>
      </div>
      <div class="card-body">
        <div id="ed-serverchan">
          <div class="field">
            <input type="text" id="serverchan-key" autocomplete="off" spellcheck="false"
                   autocapitalize="off" placeholder="SCT 开头的 SendKey">
          </div>
          <div class="hint" id="serverchan-hint">正在读取…</div>
          <div class="row3" style="margin-top:14px">
            <label class="opt" style="height:40px"><input type="checkbox" id="serverchan-enabled"> 启用 Server酱</label>
          </div>
        </div>
        <div id="ed-qmsg" hidden>
          <div class="field">
            <input type="text" id="qmsg-key" autocomplete="off" spellcheck="false"
                   autocapitalize="off" placeholder="Qmsg 酱的 key">
          </div>
          <div class="hint" id="qmsg-hint">正在读取…</div>
          <div class="row3" style="margin-top:14px">
            <label class="opt" style="height:40px"><input type="checkbox" id="qmsg-enabled"> 启用 Qmsg 酱</label>
          </div>
        </div>
        <div class="toolbar">
          <button type="button" id="btn-save" class="btn primary">保存配置</button>
        </div>
      </div>
    </section>

    <!-- 右：预览 + 测试 + 最近推送 -->
    <div class="s4 stack">
      <section class="card">
        <div class="card-head">
          <h2>📨 推送预览
            <span class="q" data-tip="按当前启用的游戏与最近一次巡检结果，模拟手机收到的推送样式；仅预览，不发送"></span>
          </h2>
          <span class="spacer"></span>
          <span class="badge" id="pv-channel">-</span>
        </div>
        <div class="card-body">
          <div class="phone-mock">
            <div class="pv-bubble">
              <div class="pv-app"><i></i>ResinLoop</div>
              <div class="pv-title" id="pv-title">-</div>
              <div class="pv-body" id="pv-body">-</div>
            </div>
          </div>
          <div class="toolbar">
            <button type="button" id="btn-test" class="btn"
                    title="用输入框里的当前值试发一条，不写盘">发送测试推送</button>
          </div>
          <div id="test-result" hidden></div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>🕘 最近推送
            <span class="q" data-tip="来自运行日志里的真实推送记录，最多显示 8 条"></span>
          </h2>
        </div>
        <div class="card-body">
          <div class="recent" id="pv-recent"><div>读取中…</div></div>
        </div>
      </section>
    </div>
  </section>

  <!-- ======================= 定时与状态 ======================= -->
  <section class="tabpanel" id="tab-schedule">
    <section class="card s12">
      <div class="card-head">
        <h2>⏰ 提醒时间点
          <span class="q" data-tip="每天到点检查一次并推送；修改立即生效，无需重启"></span>
        </h2>
        <span class="badge" id="d-count">-</span>
      </div>
      <div class="card-body">
        <div class="ruler" id="d-ruler"></div>
        <div class="chips" id="d-times"></div>
        <div class="row3">
          <input type="time" id="d-new-time" value="22:00" step="60">
          <button id="btn-add-time" class="btn">添加</button>
        </div>
      </div>
    </section>

    <section class="card s7">
      <div class="card-head">
        <h2>🧾 今日巡检动态
          <span class="q" data-tip="来自每日巡检写入的历史记录：最近 3 次有数据的检查结果"></span>
        </h2>
        <span class="spacer"></span>
        <button type="button" class="btn sm" id="btn-log-refresh">刷新</button>
      </div>
      <div id="run-log"><div class="emptybox">读取中…</div></div>
    </section>

    <section class="card s5">
      <div class="card-head">
        <h2><i class="dot" id="d-dot" style="background:var(--muted)"></i>🛡️ 守护进程</h2>
        <span class="badge" id="d-state">读取中</span>
        <span class="spacer"></span>
        <button type="button" class="mini" id="btn-daemon-start">启动</button>
        <button type="button" class="mini" id="btn-daemon-stop">停止</button>
      </div>
      <div class="card-body">
        <div class="kv-grid">
          <div><span>下次提醒</span><b id="d-next">-</b></div>
          <div><span>最近一次</span><b id="d-last">-</b></div>
          <div><span>进程 PID</span><b id="d-pid">-</b></div>
          <div><span>启动时间</span><b id="d-since">-</b></div>
          <div><span>参与检查</span><b id="d-games">-</b></div>
          <div><span>心跳</span><b id="d-heart">-</b></div>
        </div>
      </div>
    </section>

    <section class="card s12 set-card">
      <div class="card-head">
        <h2>⚙️ 开机与提醒行为</h2>
        <span class="q" data-tip="点击任意一行展开详细设置"></span>
      </div>
      <details class="set-item" id="acc-autostart">
        <summary class="set-head">
          <span class="set-lead"><i class="set-ico">💻</i>
            <span class="set-txt"><b>下次开机自动运行</b>
              <small>开机后自动启动并挂载托盘</small></span>
          </span>
          <span class="set-end">
            <span class="badge" id="autostart-state">读取中</span>
            <label class="toggle"><input type="checkbox" id="autostart-toggle"><i></i></label>
          </span>
        </summary>
        <div class="set-body">
          <div class="hint" id="autostart-hint">读取中…</div>
        </div>
      </details>

      <details class="set-item" id="acc-catchup">
        <summary class="set-head">
          <span class="set-lead"><i class="set-ico">😴</i>
            <span class="set-txt"><b>睡过头补发</b>
              <small>错过时间点后在窗口内补跑一次</small></span>
          </span>
          <span class="set-end">
            <span class="set-note" id="cu-state">-</span>
            <label class="toggle"><input type="checkbox" id="catch-up-toggle"><i></i></label>
          </span>
        </summary>
        <div class="set-body">
          <div class="row3">
            <input type="number" id="catch-up-minutes" min="0" max="720" step="30">
            <button id="btn-catch-up" class="btn">保存</button>
          </div>
          <div class="hint">单位：分钟，最大 720；0 = 不补发。</div>
        </div>
      </details>

      <details class="set-item" id="acc-resource">
        <summary class="set-head">
          <span class="set-lead"><i class="set-ico">🔔</i>
            <span class="set-txt"><b>体力溢出提醒</b>
              <small>当前体力 ≥ 阈值时额外推送一条；0 = 关闭该游戏</small></span>
          </span>
          <span class="set-end">
            <span class="set-note" id="resin-state">读取中</span>
            <label class="toggle"><input type="checkbox" id="resin-toggle"><i></i></label>
          </span>
        </summary>
        <div class="set-body">
          <div class="resin-row">
            <label><span>原神 · 树脂</span>
              <input type="number" id="resin-genshin" min="0" max="1000" step="10"
                     placeholder="160" inputmode="numeric"></label>
            <label><span>星铁 · 开拓力</span>
              <input type="number" id="resin-starrail" min="0" max="1000" step="10"
                     placeholder="240" inputmode="numeric"></label>
            <label><span>绝区零 · 电量</span>
              <input type="number" id="resin-zzz" min="0" max="1000" step="10"
                     placeholder="200" inputmode="numeric"></label>
            <label><span>方舟 · 理智</span>
              <input type="number" id="resin-arknights" min="0" max="1000" step="10"
                     placeholder="135" inputmode="numeric"></label>
            <label><span>终末地 · 理智</span>
              <input type="number" id="resin-endfield" min="0" max="1000" step="10"
                     placeholder="300" inputmode="numeric"></label>
          </div>
          <button id="btn-resin-save" class="btn">保存阈值</button>
          <div class="hint" id="resin-hint">读取中…</div>
        </div>
      </details>

      <details class="set-item" id="acc-activity">
        <summary class="set-head">
          <span class="set-lead"><i class="set-ico">⏳</i>
            <span class="set-txt"><b>活动结束提醒</b>
              <small>活动结束前设定天数时，在推送末尾追加提醒</small></span>
          </span>
          <span class="set-end">
            <span class="set-note" id="act-state">-</span>
            <label class="toggle"><input type="checkbox" id="activity-toggle"><i></i></label>
          </span>
        </summary>
        <div class="set-body">
          <div class="row3">
            <input type="number" id="activity-days" min="1" max="30" step="1"
                   inputmode="numeric" placeholder="3">
            <button id="btn-activity-days" class="btn">保存</button>
          </div>
          <div class="hint">活动结束前多少天开始提醒，1–30 天。</div>
        </div>
      </details>
    </section>
  </section>

  <!-- ======================= 底部日志控制台 ======================= -->
  </main>
  </div><!-- /app-body -->

  <!-- 固定底栏：不随内容滚动；收缩 48px / 展开 200px -->
  <details class="console" id="log-console">
    <summary class="console-head">
      <span class="console-title">🧾 运行日志</span>
      <span class="console-last" id="console-last">展开后加载真实巡检日志</span>
      <button type="button" class="mini" id="btn-console-refresh">刷新</button>
    </summary>
    <div class="console-body" id="console-body"><span class="console-empty">展开后加载…</span></div>
  </details>
</div>

<div id="toast"></div>

<script>
/* =========================== Tab 切换 =========================== */
const PANEL_NAMES = ['history', 'query', 'account', 'notify', 'schedule'];
let schedTimer = null;

function switchTab(name){
  PANEL_NAMES.forEach(function(k){
    const panel = document.getElementById('tab-' + k);
    if (panel) {
      panel.classList.toggle('on', k === name);
    }
    const btn = document.querySelector('.nav button[data-tab="' + k + '"]');
    if (btn) btn.classList.toggle('on', k === name);
  });

  if (name === 'query') loadQuery();
  if (name === 'notify') { loadPush(); loadPushPreview(); loadPushRecent(); }
  if (name === 'schedule') { loadSchedule(); startSchedPoll(); loadThresholds(); loadAutostart(); loadRunLog(); } else { stopSchedPoll(); }
  if (name === 'account') { loadDevice(); loadAccounts(); refreshNotice(); }
  if (name === 'history') { loadHistory(); loadLive(false); }
}

function startSchedPoll(){
  stopSchedPoll();
  schedTimer = setInterval(loadSchedule, 3000);
}
function stopSchedPoll(){
  if (schedTimer) { clearInterval(schedTimer); schedTimer = null; }
}

document.querySelectorAll('.nav button[data-tab]').forEach(function(btn){
  btn.addEventListener('click', function(){ switchTab(this.dataset.tab); });
});

document.addEventListener('click', function(e){
  const link = e.target.closest('[data-goto]');
  if (!link) return;
  e.preventDefault();
  switchTab(link.dataset.goto);
});

/* =========================== 通用提示 =========================== */
function toast(text, isErr){
  const el = document.getElementById('toast');
  el.textContent = text;
  el.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(function(){ el.className = ''; }, 2800);
}

async function postJSON(url, payload){
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {})
  });
  return r.json();
}

function escHtml(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

/* =========================== 账号绑定 =========================== */
let sid = null, timer = null;

/* 选择卡视觉与 input 状态严格同步：.on = 开启 */
function syncSelCard(box){
  const card = box.closest('.sel-card');
  if (card) card.classList.toggle('on', box.checked);
}

function toggleAll(group, checked){
  document.querySelectorAll('input[name="' + group + '"]').forEach(function(i){
    i.checked = checked;
    syncSelCard(i);
  });
  updateLoginButtons();
  pushEnabled(group,
    [...document.querySelectorAll('input[name="' + group + '"]')].map(function(i){ return i.value; }),
    checked);
}

/* 标题行右侧的「全选」次级按钮：未全开 → 全开；已全开 → 取消全选 */
function toggleAllCards(){
  const boxes = [...document.querySelectorAll('.sel-card input')];
  const target = !boxes.every(function(b){ return b.checked; });
  ['m', 's'].forEach(function(g){
    const gb = [...document.querySelectorAll('input[name="' + g + '"]')];
    if (gb.some(function(b){ return b.checked !== target; })) toggleAll(g, target);
  });
}

function pushEnabled(group, games, enabled){
  postJSON('/api/accounts/enabled', { games: games, enabled: enabled })
    .then(function(d){
      if (!d || !d.ok) { toast((d && d.message) || '设置失败', true); loadAccounts(); return; }
      toast(d.message || (enabled ? '已开启提醒' : '已关闭提醒'));
    })
    .catch(function(){ toast('无法连接本地服务', true); });
}

/* 「全选」按钮文案随状态变化 */
function updateLoginButtons(){
  const boxes = [...document.querySelectorAll('.sel-card input')];
  const allOn = boxes.length > 0 && boxes.every(function(b){ return b.checked; });
  const btn = document.getElementById('btn-selall');
  if (btn) btn.textContent = allOn ? '取消全选' : '全选';
}

/* 从后端拉取真实的巡检开关与凭证状态，覆盖页面默认值 */
function loadAccounts(){
  fetch('/api/accounts').then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok) return;
    (d.items || []).forEach(function(it){
      const box = document.querySelector(
        'input[name="m"][value="' + it.id + '"],input[name="s"][value="' + it.id + '"]');
      if (!box) return;
      box.checked = !!it.enabled;
      syncSelCard(box);
    });
    renderCredHealth(d);
    renderKpiCred(d);
    renderCapsuleDots(d);
    updateLoginButtons();
  }).catch(function(){});
}

/* 胶囊黄点：该平台任一游戏未绑定凭证 → 点亮 */
function renderCapsuleDots(d){
  ['m', 's'].forEach(function(g){
    const dot = document.getElementById('cap-dot-' + g);
    if (!dot) return;
    const missing = (d.items || []).some(function(it){
      const box = document.querySelector('input[name="' + g + '"][value="' + it.id + '"]');
      return box && !it.has_credential;
    });
    dot.classList.toggle('on', missing);
  });
}

/* 凭证健康：紧凑列表。绿点 = 已绑定 · 黄点 = 早期绑定（无更新记录）· 红点 = 未绑定
   色彩纪律：正常 = 灰字，异常（未绑定）= 红字小标 */
function relTime(text){
  // "2026-10-27 22:00:33" -> "3天前"
  const m = String(text || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return String(text || '');
  const t = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  const diff = Date.now() - t.getTime();
  if (diff < 0) return m[2] + '-' + m[3];
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return min + '分钟前';
  const h = Math.floor(min / 60);
  if (h < 24) return h + '小时前';
  const d = Math.floor(h / 24);
  if (d < 30) return d + '天前';
  return m[2] + '-' + m[3];
}

function renderCredHealth(d){
  const box = document.getElementById('cred-health');
  const badge = document.getElementById('cred-health-badge');
  if (!box) return;
  const items = (d && d.items) || [];
  if (!items.length) {
    box.innerHTML = '<div class="emptybox">config.json 里没有账号节点。</div>';
    if (badge) badge.textContent = '-';
    return;
  }
  const bound = items.filter(function(i){ return i.has_credential; }).length;
  if (badge) {
    badge.textContent = bound + '/' + items.length + ' 已绑定';
    badge.className = 'badge' + (bound === items.length ? '' : ' warn');
  }
  box.innerHTML = '<div class="cred-list">' +
    items.map(function(it){
      const st = !it.has_credential ? 'err'
               : (it.credential_updated_at ? 'ok' : 'warn');
      const stText = st === 'err' ? '未绑定' : (st === 'warn' ? '早期绑定' : '已绑定');
      return '<div class="cred-row' + (st === 'err' ? ' bad' : '') + '">' +
        selIconHtml(it.id, it.name || it.id) +
        '<div class="cred-info"><b>' + escHtml(it.name || it.id) + '</b>' +
        '<small>' + stText +
          (it.nickname ? ' · ' + escHtml(it.nickname) : '') + '</small></div>' +
        '<span class="cred-dot ' + st + '"></span>' +
        '<span class="cred-time">' +
          (it.credential_updated_at ? relTime(it.credential_updated_at) : '—') +
        '</span>' +
        '</div>';
    }).join('') + '</div>';
}

/* 选择卡/凭证行的游戏图标：有官方图用图，没有（终末地）退回首字占位 */
function selIconHtml(gid, name){
  const base = GAME_ICON[gid];
  if (!base) {
    return '<span class="sel-ico ph">' + escHtml(String(name || gid || '?').slice(0, 1)) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="sel-ico" alt="" src="' + base + '.png' + v +
         '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

/* 把静态占位图替换成官方图标（GAME_ICON 定义后调用一次） */
function fillSelIcons(){
  document.querySelectorAll('.sel-card').forEach(function(card){
    const ph = card.querySelector('.sel-ico.ph');
    if (!ph) return;
    const input = card.querySelector('input');
    if (!input) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = selIconHtml(input.value, ph.textContent);
    const node = tmp.firstChild;
    if (node) ph.replaceWith(node);
  });
}

document.querySelectorAll('.sel-card input').forEach(function(box){
  syncSelCard(box);
  box.addEventListener('change', function(){
    syncSelCard(box);
    updateLoginButtons();
    pushEnabled(box.name, [box.value], box.checked);
  });
});

function badgeFor(state){
  return {
    creating:['准备中',''], waiting:['等待扫码','warn'], scanned:['已扫码','warn'],
    writing:['写入中',''], done:['完成','ok'], error:['失败','err']
  }[state] || [state,''];
}

function setBusy(busy){
  ['cap-m', 'cap-s'].forEach(function(id){
    const b = document.getElementById(id);
    if (b) b.disabled = busy;
  });
}

function render(d){
  const stage = document.getElementById('stage');
  stage.hidden = false;
  const pair = badgeFor(d.state);
  const badge = document.getElementById('badge');
  badge.textContent = pair[0];
  badge.className = 'badge ' + pair[1];
  document.getElementById('msg').textContent = d.message || '';

  const box = document.getElementById('qrbox');
  if (d.qr) {
    if (!box.querySelector('img')) box.innerHTML = '<img alt="登录二维码">';
    box.querySelector('img').src = 'data:image/png;base64,' + d.qr;
  } else if (d.state === 'creating' || d.state === 'writing') {
    box.innerHTML = '<span class="ph"><span class="spin"></span>请稍候…</span>';
  }

  const kv = document.getElementById('detail');
  const items = [];
  if (d.detail) {
    if (d.detail.ltuid) items.push('米游社 ID：<code>' + d.detail.ltuid + '</code>');
    if (d.detail.games) items.push('已更新：' + d.detail.games.join('、'));
    if (d.detail.note) items.push(d.detail.note);
  }
  if (d.results && d.results.length && (d.results.length > 1 || d.state === 'error')) {
    const rows = d.results.map(function(r){
      return '<div><span class="' + (r.ok ? 'i-ok' : 'i-no') + '">' +
             (r.ok ? '✓' : '✗') + '</span>' + r.label +
             (r.note ? ' — ' + r.note : '') + '</div>';
    }).join('');
    items.push('<div class="res">' + rows + '</div>');
  }
  kv.innerHTML = items.join('<br>');
  kv.hidden = items.length === 0;
}

async function start(provider, targets){
  reset();
  setBusy(true);
  document.getElementById('modal-title').textContent =
    provider === 'mihoyo' ? '米游社扫码登录' : '森空岛扫码登录';
  const url = '/api/start?provider=' + encodeURIComponent(provider)
            + '&targets=' + encodeURIComponent(targets.join(','));
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (!d.ok) { showError(d.message); setBusy(false); return; }
    sid = d.sid;
    poll();
  } catch (e) {
    showError('无法连接本地服务：' + e);
    setBusy(false);
  }
}

/* 扫码目标：优先当前开启监控的游戏；一个都没开则扫该平台全部游戏 */
function providerTargets(group){
  const boxes = [...document.querySelectorAll('input[name="' + group + '"]')];
  const on = boxes.filter(function(b){ return b.checked; }).map(function(b){ return b.value; });
  return on.length ? on : boxes.map(function(b){ return b.value; });
}

function startMihoyo(){
  start('mihoyo', providerTargets('m'));
}

function startSkland(){
  start('skland', providerTargets('s'));
}

function showError(msg){
  const stage = document.getElementById('stage');
  stage.hidden = false;
  const badge = document.getElementById('badge');
  badge.textContent = '失败'; badge.className = 'badge err';
  document.getElementById('msg').textContent = msg;
  document.getElementById('qrbox').innerHTML = '<span class="ph">未能创建二维码</span>';
}

function poll(){
  fetch('/api/state?sid=' + encodeURIComponent(sid))
    .then(function(r){ return r.json(); })
    .then(function(d){
      render(d);
      if (d.state === 'done' || d.state === 'error') { setBusy(false); loadAccounts(); return; }
      timer = setTimeout(poll, 1000);
    })
    .catch(function(){ timer = setTimeout(poll, 2000); });
}

function reset(){
  if (timer) { clearTimeout(timer); timer = null; }
  sid = null;
  setBusy(false);
  document.getElementById('stage').hidden = true;
  document.getElementById('qrbox').innerHTML = '<span class="ph">二维码准备中…</span>';
  document.getElementById('detail').hidden = true;
}

function refreshNotice(){
  fetch('/api/env').then(function(r){ return r.json(); }).then(function(d){
    const box = document.getElementById('notice');
    if (!box) return;
    if (d.configured) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    box.innerHTML =
      '<b>尚未配置推送通道</b>，手机收不到提醒。去 <a data-goto="notify">「通知绑定」</a> 填写。';
  }).catch(function(){});
}

/* ---------- 设备伪装 ---------- */
function renderDevice(d){
  if (!d.ok) { toast(d.message || '读取设备档案失败', true); return; }
  const p = d.profile || {};
  document.getElementById('dev-badge').textContent = d.custom ? '自定义档案' : '默认档案';
  document.getElementById('dev-badge').className = 'badge ' + (d.custom ? 'ok' : '');
  document.getElementById('dev-current').innerHTML =
    '当前设备：<b>' + (p.brand || '?') + ' ' + (p.model || '?') + '</b>' +
    ' · Android ' + (p.os_version || '?') +
    ' · OAID <code>' + (p.oaid || '?') + '</code>' +
    ' · 指纹缓存' + (d.cache_exists ? '存在' : '<b>已清空</b>');

  const apk = d.apk || {};
  const qrBox = document.getElementById('apk-qr');
  const meta = document.getElementById('apk-meta');
  if (apk.available && apk.qr) {
    qrBox.innerHTML = '<img alt="APK 下载二维码">';
    qrBox.querySelector('img').src = 'data:image/png;base64,' + apk.qr;
    let shown = apk.url;
    try { shown = decodeURIComponent(apk.url); } catch (e) {}
    meta.innerHTML = apk.name + '（' + apk.size_text + '）<br>' + shown +
      (apk.warning ? '<br><span style="color:var(--warn)">⚠ ' + apk.warning + '</span>' : '');
  } else {
    qrBox.innerHTML =
      '<div style="padding:10px 8px;text-align:center">' +
      '<div style="font-size:11px;color:var(--muted);line-height:1.5">二维码不可用</div>' +
      '<button class="btn sm" id="btn-apk-retry" style="margin-top:6px">换个端口重试</button>' +
      '</div>';
    meta.innerHTML = '<span style="color:var(--warn)">' + (apk.message || '未找到 APK') + '</span>';
    const btn = document.getElementById('btn-apk-retry');
    if (btn) btn.addEventListener('click', restartApkService);
  }
}

async function restartApkService(){
  const btn = document.getElementById('btn-apk-retry');
  if (btn) { btn.disabled = true; btn.textContent = '重试中…'; }
  try {
    const d = await postJSON('/api/apk/restart', {});
    const box = document.getElementById('apk-qr');
    const meta = document.getElementById('apk-meta');
    if (d.available && d.qr) {
      box.innerHTML = '<img alt="APK 下载二维码">';
      box.querySelector('img').src = 'data:image/png;base64,' + d.qr;
      let shown = d.url;
      try { shown = decodeURIComponent(d.url); } catch (e) {}
      meta.innerHTML = d.name + '（' + d.size_text + '）<br>' + shown;
      toast('下载服务已重启，端口 ' + d.port);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '换个端口重试'; }
      meta.innerHTML = '<span style="color:var(--warn)">' + (d.message || '仍然不可用') + '</span>';
      toast('仍然不可用：' + (d.message || ''), true);
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '换个端口重试'; }
    toast('重启失败：' + e.message, true);
  }
}

function loadDevice(){
  fetch('/api/device').then(function(r){ return r.json(); })
    .then(renderDevice)
    .catch(function(){ toast('无法读取设备档案', true); });
}

/* =========================== 体力溢出阈值 =========================== */
function renderThresholds(d){
  if (!d || !d.ok) { return; }
  const values = d.values || {};
  const defs = d.defaults || {};
  ['genshin','starrail','zzz','arknights','endfield'].forEach(function(gid){
    const box = document.getElementById('resin-' + gid);
    if (!box) return;
    box.value = (values[gid] != null) ? values[gid] : '';
    box.placeholder = (defs[gid] != null) ? String(defs[gid]) : '0';
  });

  const enabled = d.enabled !== false;
  const box = document.getElementById('resin-toggle');
  if (box) box.checked = enabled;

  const state = document.getElementById('resin-state');
  if (state) {
    const off = Object.keys(values).filter(function(k){ return values[k] === 0; });
    state.textContent = !enabled ? '已关闭'
      : (off.length ? off.length + ' 项关闭' : '已启用');
  }
  const item = document.getElementById('acc-resource');
  if (item) item.classList.toggle('is-off', !enabled);

  const hint = document.getElementById('resin-hint');
  if (hint) hint.textContent = '留空用默认值 · 0 = 关闭该游戏';
}

function loadThresholds(){
  fetch('/api/settings').then(function(r){ return r.json(); })
    .then(renderThresholds)
    .catch(function(){
      const hint = document.getElementById('resin-hint');
      if (hint) hint.textContent = '读取失败，请刷新重试';
    });
}

function resinPayload(){
  const box = document.getElementById('resin-toggle');
  return {
    genshin:   document.getElementById('resin-genshin').value,
    starrail:  document.getElementById('resin-starrail').value,
    zzz:       document.getElementById('resin-zzz').value,
    arknights: document.getElementById('resin-arknights').value,
    endfield:  document.getElementById('resin-endfield').value,
    enabled:   box ? box.checked : true
  };
}

async function saveThresholds(){
  const btn = document.getElementById('btn-resin-save');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/settings', resinPayload());
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderThresholds(d);
    toast(d.message || '已保存');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function toggleResourceAlert(){
  const box = document.getElementById('resin-toggle');
  if (!box) return;
  const want = box.checked;
  box.disabled = true;
  try {
    // 顺带把输入框里的当前值一起提交，避免开关切换把用户刚改的数字丢掉
    const payload = resinPayload();
    payload.enabled = want;
    const d = await postJSON('/api/settings', payload);
    if (!d.ok) {
      box.checked = !want;
      toast(d.message || '设置失败', true);
      return;
    }
    renderThresholds(d);
    toast(want ? '已开启体力溢出提醒' : '已关闭体力溢出提醒（阈值已保留）');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

document.getElementById('btn-resin-save').addEventListener('click', saveThresholds);

async function saveDevice(){
  const btn = document.getElementById('btn-dev-save');
  const text = document.getElementById('dev-text').value;
  if (!text.trim()) { toast('请先粘贴设备信息', true); return; }
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/device', { text: text });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderDevice(d);
    document.getElementById('dev-text').value = '';
    toast(d.message || '设备指纹已替换，请重新扫码');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function resetDevice(){
  if (!confirm('恢复内置默认设备档案？恢复后需要重新扫码。')) return;
  try {
    const d = await postJSON('/api/device/reset', {});
    if (!d.ok) { toast(d.message || '恢复失败', true); return; }
    renderDevice(d);
    toast(d.message || '已恢复默认');
  } catch (e) {
    toast('恢复失败：' + e, true);
  }
}

/* =========================== 通知绑定 =========================== */
const CHANNELS = ['serverchan', 'qmsg'];

function hintText(it){
  if (!it.has_key && !it.enabled) return '未配置';
  if (!it.has_key) return '已启用，但没有 Key';
  if (!it.in_channels) return 'Key 已保存，未启用';
  return '已配置并生效';
}

function renderPush(d){
  CHANNELS.forEach(function(name){
    const it = (d.items || {})[name] || {};
    const box = document.getElementById(name + '-key');
    const en = document.getElementById(name + '-enabled');
    const hint = document.getElementById(name + '-hint');
    if (!box || !en || !hint) return;
    box.dataset.masked = it.masked || '';
    box.value = it.masked || '';
    box.placeholder = it.has_key ? ''
      : (name === 'serverchan' ? 'SCT 开头的 SendKey' : 'Qmsg 酱的 key');
    en.checked = !!it.enabled;
    hint.innerHTML = hintText(it);
  });
  const an = document.getElementById('always-notify');
  if (an) an.checked = !!d.always_notify;

  // 左栏渠道列表：点 = 配置健康度（异常=橙），文字 = 状态说明
  CHANNELS.forEach(function(name){
    const it = (d.items || {})[name] || {};
    const stateEl = document.getElementById('ch-state-' + name);
    const dotEl = document.getElementById('ch-dot-' + name);
    const healthy = !!(it.has_key && it.enabled && it.in_channels);
    if (stateEl) stateEl.textContent = hintText(it);
    if (dotEl) dotEl.className = 'ch-dot' + (healthy ? '' : ' bad');
  });
  refreshNotice();
}

function loadPush(){
  fetch('/api/push').then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok) { toast(d.message || '读取配置失败', true); return; }
    renderPush(d);
  }).catch(function(){ toast('无法连接本地服务', true); });
}

/* ---------- 三区结构：渠道列表切换 + 最近推送记录 ---------- */
const CHANNEL_NAMES = {
  serverchan: '💬 Server酱（微信）',
  qmsg: '🐧 Qmsg 酱（QQ）'
};
let selChannel = 'serverchan';

function selectChannel(name){
  if (!CHANNEL_NAMES[name]) return;
  selChannel = name;
  document.querySelectorAll('.ch-item').forEach(function(el){
    el.classList.toggle('on', el.dataset.ch === name);
  });
  document.getElementById('ed-serverchan').hidden = name !== 'serverchan';
  document.getElementById('ed-qmsg').hidden = name !== 'qmsg';
  // 重建标题 + (?) 提示（textContent 会把图标一起清掉）
  document.getElementById('ed-title').innerHTML =
    escHtml(CHANNEL_NAMES[name]) +
    ' <span class="q" data-tip="Key 由后端打码显示：不修改输入框 = 保持原值；清空后保存 = 删除该 Key"></span>';
}

document.querySelectorAll('.ch-item').forEach(function(el){
  el.addEventListener('click', function(){ selectChannel(this.dataset.ch); });
});

/* 最近推送记录：从运行日志里筛推送相关行（真实记录，最多 8 条） */
function loadPushRecent(){
  const box = document.getElementById('pv-recent');
  if (!box) return;
  fetch('/api/logtail?n=300').then(function(r){ return r.json(); })
    .then(function(d){
      if (!d || d.ok === false || !(d.lines || []).length) {
        box.innerHTML = '<div>还没有推送记录。</div>';
        return;
      }
      const rows = d.lines.filter(function(l){ return /推送|should_push|推送决策/.test(l); })
        .slice(-8).reverse();
      if (!rows.length) {
        box.innerHTML = '<div>最近没有推送记录。</div>';
        return;
      }
      box.innerHTML = rows.map(function(l){
        return '<div>' + escHtml(l.replace(/^\d{4}-\d{2}-\d{2} /, '')) + '</div>';
      }).join('');
    })
    .catch(function(){ box.innerHTML = '<div>读取日志失败，请稍后重试。</div>'; });
}

function collectPush(){
  const out = {};
  CHANNELS.forEach(function(name){
    const box = document.getElementById(name + '-key');
    const en = document.getElementById(name + '-enabled');
    out[name] = { enabled: en.checked, key: box.value };
  });
  const an = document.getElementById('always-notify');
  if (an) out.always_notify = an.checked;
  return out;
}

async function savePush(){
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/push', collectPush());
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderPush(d);
    document.getElementById('test-result').hidden = true;
    toast(d.message || '保存成功');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function testPush(){
  const btn = document.getElementById('btn-test');
  const box = document.getElementById('test-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '发送中…';
  box.hidden = true;
  try {
    const d = await postJSON('/api/test_push', collectPush());
    if (!d.ok) { toast(d.message || '发送失败', true); return; }
    const rows = (d.results || []).map(function(x){
      return '<div><span class="' + (x.ok ? 'i-ok' : 'i-no') + '">' +
             (x.ok ? '✓' : '✗') + '</span>' + x.label + ' — ' + x.message + '</div>';
    }).join('');
    box.innerHTML = '<div class="res">' + rows + '</div>';
    box.hidden = false;
    const allOk = (d.results || []).every(function(x){ return x.ok; });
    toast(allOk ? '测试消息已发出' : '部分通道发送失败', !allOk);
  } catch (e) {
    toast('发送失败：' + e, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

CHANNELS.forEach(function(name){
  const box = document.getElementById(name + '-key');
  if (!box) return;
  box.addEventListener('focus', function(){
    if (box.value && box.value === box.dataset.masked) box.select();
  });
});

document.getElementById('btn-save').addEventListener('click', savePush);
document.getElementById('btn-test').addEventListener('click', testPush);

/* =========================== 定时与状态 =========================== */
function renderDaemon(st){
  const dot = document.getElementById('d-dot');
  const badge = document.getElementById('d-state');
  const running = !!st.running;
  /* 色彩纪律：运行中 = 灰点灰字，停止才是红色 */
  dot.style.background = running ? 'var(--muted)' : 'var(--err)';
  badge.textContent = running ? '运行中' : '已停止';
  badge.className = 'badge' + (running ? '' : ' err');

  document.getElementById('d-pid').textContent = st.pid || '-';
  document.getElementById('d-since').textContent = st.started_at || '-';
  const heart = document.getElementById('d-heart');
  if (heart) {
    heart.textContent = (st.heartbeat_age != null)
      ? Math.round(st.heartbeat_age) + ' 秒前' : '-';
  }

  const last = st.last_run || {};
  document.getElementById('d-last').textContent =
    last.at ? (last.exit === 0 ? '成功' : '退出码 ' + last.exit) + ' (' + shortStamp(last.at) + ')'
            : '还没跑过';
  document.getElementById('btn-daemon-start').disabled = running;
  document.getElementById('btn-daemon-stop').disabled = !running;
}

function shortStamp(text){
  // "2026-10-27 01:23:43" -> "10-27 01:23"
  const m = String(text || '').match(/^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? (m[1] + ' ' + m[2]) : String(text || '');
}

function renderSchedule(d){
  if (!d.ok) { toast(d.message || '读取失败', true); return; }
  const times = d.times || [];
  const box = document.getElementById('d-times');
  if (!times.length) {
    box.innerHTML = '<span class="chip empty">还没有时间点</span>';
  } else {
    box.innerHTML = times.map(function(t){
      return '<span class="chip">' + t +
             '<button title="删除" data-remove="' + t + '">×</button></span>';
    }).join('');
  }
  renderRuler(times);
  document.getElementById('d-count').textContent = times.length + ' 个';
  document.getElementById('d-next').textContent = d.next_run || '-';
  renderKpiNext(d);

  // 参与检查的账号：只给数量，完整清单走 title（来自后端，不写死）
  const gamesBox = document.getElementById('d-games');
  if (gamesBox) {
    const infos = d.accounts_info || [];
    if (!infos.length) {
      gamesBox.textContent = '无';
      gamesBox.title = 'config.json 里 accounts 的 enabled 全为 false';
    } else {
      gamesBox.textContent = infos.length + ' 个';
      gamesBox.title = infos.map(function(it){
        return (it.name || it.id) + (it.nickname ? '（' + it.nickname + '）' : '');
      }).join('\n');
    }
  }

  renderCatchUp(d);
  renderDaemon(d.daemon || {});
  renderActivitySwitch(d.activity_alert_enabled, d.activity_alert_days);
}

/* 活动结束提醒：config.app.activity_alert_enabled / activity_alert_days */
function renderActivitySwitch(enabled, days){
  const on = enabled !== false;
  const box = document.getElementById('activity-toggle');
  if (box) box.checked = on;
  const note = document.getElementById('act-state');
  if (note) note.textContent = on ? '已开启' : '已关闭';
  const item = document.getElementById('acc-activity');
  if (item) item.classList.toggle('is-off', !on);
  const num = parseInt(days, 10);
  const input = document.getElementById('activity-days');
  if (input && document.activeElement !== input && !isNaN(num)) input.value = num;
}

async function toggleActivityAlert(){
  const box = document.getElementById('activity-toggle');
  if (!box) return;
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/activity_alert', { enabled: want });
    if (!d.ok) { box.checked = !want; toast(d.message || '设置失败', true); return; }
    renderActivitySwitch(d.activity_alert_enabled);
    toast(want ? '已开启活动结束提醒' : '已关闭活动结束提醒');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

/* 补发：开关 + 窗口数值分别对应 config.app.catch_up_enabled / catch_up_minutes */
let lastCatchUp = { enabled: true, minutes: 180 };

function renderCatchUp(d){
  d = d || {};
  const enabled = d.catch_up_enabled !== false;
  const num = parseInt(d.catch_up_minutes, 10);
  const minutes = isNaN(num) ? 180 : num;
  lastCatchUp = { enabled: enabled, minutes: minutes };

  const input = document.getElementById('catch-up-minutes');
  if (input && document.activeElement !== input) input.value = minutes;

  const box = document.getElementById('catch-up-toggle');
  if (box) box.checked = enabled;

  const badge = document.getElementById('cu-state');
  if (badge) {
    if (!enabled) badge.textContent = '已关闭';
    else if (minutes > 0) badge.textContent = minutes + ' 分钟内补发';
    else badge.textContent = '窗口为 0';
  }
  const item = document.getElementById('acc-catchup');
  if (item) item.classList.toggle('is-off', !enabled);
}

async function toggleCatchUp(){
  const box = document.getElementById('catch-up-toggle');
  if (!box) return;
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/catch_up',
      { minutes: lastCatchUp.minutes, enabled: want });
    if (!d.ok) {
      box.checked = !want;
      toast(d.message || '设置失败', true);
      return;
    }
    renderCatchUp(d);
    toast(want ? '已开启睡过头补发' : '已关闭睡过头补发（窗口数值已保留）');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

async function saveCatchUp(){
  const btn = document.getElementById('btn-catch-up');
  const input = document.getElementById('catch-up-minutes');
  const raw = parseInt(input.value, 10);
  let minutes = isNaN(raw) ? 0 : raw;
  if (minutes < 0) minutes = 0;
  if (minutes > 720) minutes = 720;
  btn.disabled = true;
  try {
    const d = await postJSON('/api/catch_up',
      { minutes: minutes, enabled: lastCatchUp.enabled });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderCatchUp(d);
    toast(d.message || '已保存');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
  }
}

function loadSchedule(){
  fetch('/api/schedule').then(function(r){ return r.json(); })
    .then(renderSchedule)
    .catch(function(){});
}

/* ========================= 开机自启 ========================= */
function renderAutostart(d){
  const box = document.getElementById('autostart-toggle');
  const badge = document.getElementById('autostart-state');
  const hint = document.getElementById('autostart-hint');
  const supported = !!(d && d.supported);

  if (box) {
    box.checked = !!(d && d.enabled);
    box.disabled = !supported;
  }
  if (badge) {
    if (!supported) {
      badge.textContent = '不支持';
      badge.className = 'badge err';
    } else if (d.stale) {
      badge.textContent = '路径已失效';
      badge.className = 'badge err';
    } else if (d.enabled) {
      badge.textContent = '已开启';
      badge.className = 'badge';
    } else {
      badge.textContent = '未开启';
      badge.className = 'badge';
    }
  }
  if (hint) {
    if (!supported) {
      hint.textContent = (d && d.message) || '仅支持 Windows';
    } else if (d.stale) {
      hint.innerHTML = '注册表路径已失效：把开关关掉再打开一次即可刷新。';
    } else if (d.enabled) {
      hint.innerHTML = '下次登录 Windows 自动在托盘启动。';
    } else {
      hint.innerHTML = '勾选后下次登录 Windows 自动在托盘启动。';
    }
  }
}

function loadAutostart(){
  fetch('/api/autostart').then(function(r){ return r.json(); })
    .then(renderAutostart)
    .catch(function(){
      const hint = document.getElementById('autostart-hint');
      if (hint) hint.textContent = '读取自启状态失败，刷新页面重试';
    });
}

async function toggleAutostart(){
  const box = document.getElementById('autostart-toggle');
  if (!box) return;
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/autostart', { enable: want });
    if (!d.ok) {
      box.checked = !want;          // 失败就把开关拨回去，别让界面撒谎
      toast(d.message || '设置失败', true);
      renderAutostart(d);
      return;
    }
    renderAutostart(d);
    toast(d.message || (want ? '已开启开机自启' : '已关闭开机自启'));
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

document.getElementById('autostart-toggle')
  .addEventListener('change', toggleAutostart);
document.getElementById('catch-up-toggle')
  .addEventListener('change', toggleCatchUp);

/* 活动提醒提前天数：单独保存，不碰总开关 */
async function saveActivityDays(){
  const input = document.getElementById('activity-days');
  const btn = document.getElementById('btn-activity-days');
  if (!input || !btn) return;
  const raw = input.value;
  const num = parseInt(raw, 10);
  if (isNaN(num) || num < 1 || num > 30) {
    toast('请填 1–30 之间的整数', true);
    return;
  }
  btn.disabled = true;
  try {
    const d = await postJSON('/api/activity_alert', { days: num });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    if (document.activeElement !== input) input.value = d.activity_alert_days;
    toast(d.message || ('活动结束前 ' + d.activity_alert_days + ' 天开始提醒'));
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-activity-days')
  .addEventListener('click', saveActivityDays);
document.getElementById('resin-toggle')
  .addEventListener('change', toggleResourceAlert);
document.getElementById('activity-toggle')
  .addEventListener('change', toggleActivityAlert);
document.getElementById('btn-live-refresh')
  .addEventListener('click', function(){ loadLive(true); });
document.getElementById('btn-act-refresh')
  .addEventListener('click', refreshActivities);
/* 活动卡片是动态渲染的，删除按钮与抽屉把手用事件委托挂在容器上 */
document.getElementById('act-list').addEventListener('click', function(e){
  if (!e.target.closest) return;
  const handle = e.target.closest('.act-drawer-handle');
  if (handle) {                                   // 「已完成」抽屉：点击展开 / 再点收起
    const drawer = handle.closest('.act-drawer');
    if (drawer) drawer.classList.toggle('open');
    return;
  }
  const btn = e.target.closest('.act-del');
  if (btn) { e.preventDefault(); deleteActivity(btn); }
});

/* 折叠面板标题栏里的开关 / 小按钮：只做自己的事，不连带展开或收起面板。 */
document.querySelectorAll('.set-head .toggle, .set-head .mini, .set-head .btn')
  .forEach(function(el){
    el.addEventListener('click', function(e){ e.stopPropagation(); });
  });

async function saveTimes(times){
  try {
    const d = await postJSON('/api/schedule', { times: times });
    if (!d.ok) { toast(d.message || '保存失败', true); return false; }
    renderSchedule(d);
    toast(d.message || '已保存');
    return true;
  } catch (e) {
    toast('保存失败：' + e, true);
    return false;
  }
}

async function addTime(){
  const input = document.getElementById('d-new-time');
  const value = (input.value || '').trim();
  if (!value) { toast('请先选择时间', true); return; }
  const current = currentTimes();
  if (current.indexOf(value) >= 0) { toast('这个时间点已经有了', true); return; }
  await saveTimes(current.concat([value]));
}

function currentTimes(){
  return [...document.querySelectorAll('#d-times .chip')]
    .map(function(el){ return el.textContent.replace('×','').trim(); })
    .filter(function(t){ return /^\d{1,2}:\d{2}$/.test(t); });
}

document.getElementById('d-times').addEventListener('click', async function(e){
  const btn = e.target.closest('[data-remove]');
  if (!btn) return;
  const target = btn.dataset.remove;
  const current = currentTimes();
  if (current.length <= 1) { toast('至少要保留一个时间点', true); return; }
  await saveTimes(current.filter(function(t){ return t !== target; }));
});

document.getElementById('btn-add-time').addEventListener('click', addTime);
document.getElementById('btn-catch-up').addEventListener('click', saveCatchUp);

async function controlDaemon(action){
  const btn = document.getElementById(action === 'start' ? 'btn-daemon-start' : 'btn-daemon-stop');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = action === 'start' ? '启动中…' : '停止中…';
  try {
    const d = await postJSON('/api/daemon/' + action, {});
    if (d.daemon) renderDaemon(d.daemon);
    const okMsg = action === 'stop'
      ? '已停止本次运行（开机自启设置不变）'
      : (d.message || '已启动守护进程');
    toast(d.ok ? okMsg : (d.message || '操作失败'), !d.ok);
  } catch (e) {
    toast('操作失败：' + e, true);
  } finally {
    btn.textContent = old;
    loadSchedule();
  }
}

document.getElementById('btn-daemon-start').addEventListener('click', function(){ controlDaemon('start'); });
document.getElementById('btn-daemon-stop').addEventListener('click', function(){ controlDaemon('stop'); });
document.getElementById('btn-dev-save').addEventListener('click', saveDevice);
document.getElementById('btn-dev-reset').addEventListener('click', resetDevice);


/* ========================= 信息查询 ========================= */
let liveLoaded = false;

function loadQuery(){
  loadActivities();
  if (!liveLoaded) loadLive(false);
}

/* 把 /api/live 结果整理成 gid → {finished, tasks, 体力信息} 的映射。
   只有查询成功（g.ok）的游戏才进入映射 —— 它的完成状态有"发言权"，
   KPI 与迷你卡以它为准；查询失败的没有记录，回落到巡检历史。 */
function buildLiveMap(d){
  const map = {};
  (d.games || []).forEach(function(g){
    if (!g.ok) return;
    map[g.game] = {
      finished: !!g.finished,
      tasks: (g.tasks || []).map(function(t){
        return { name: t.name, done: !!t.done, progress: t.progress || '' };
      }),
    };
    if (g.resource && g.resource.percent != null) {
      map[g.game].name = g.resource.name;
      map[g.game].current = g.resource.current;
      map[g.game].maximum = g.resource.maximum;
      map[g.game].percent = g.resource.percent;
    }
  });
  return map;
}

/* 任务明细 → "委托 0/4 · 实训 0/500" 式进度串；无进度语义的任务用 ✓/✗ */
function taskProgressHtml(tasks){
  const parts = (tasks || []).map(function(t){
    const label = escHtml(t.name);
    if (t.progress) {
      const hit = /(\d+)\s*\/\s*(\d+)/.exec(t.progress);
      const txt = hit ? (parseInt(hit[1], 10) + '/' + parseInt(hit[2], 10)) : escHtml(t.progress);
      return '<span class="tp-item' + (t.done ? '' : ' tp-miss') + '">' + label +
             ' <b class="mono">' + txt + '</b></span>';
    }
    return '<span class="tp-item' + (t.done ? '' : ' tp-miss') + '">' + label +
           ' <b>' + (t.done ? '✓' : '✗') + '</b></span>';
  });
  return parts.length ? '<div class="gcard-tasks">' + parts.join('<i class="tp-sep">·</i>') + '</div>' : '';
}

function renderLive(d){
  const box = document.getElementById('live-grid');
  const hint = document.getElementById('live-hint');
  if (!box) return;
  if (!d || d.ok === false) {
    box.innerHTML = '<div class="emptybox">' + escHtml((d && d.message) || '查询失败，请重试') + '</div>';
    if (hint) hint.textContent = '';
    return;
  }
  window.__liveMap = Object.assign(window.__liveMap || {}, buildLiveMap(d));
  const games = d.games || [];
  if (!games.length) {
    box.innerHTML = '<div class="emptybox">没有启用的账号。</div>';
  } else {
    box.innerHTML = '<table class="tbl"><thead><tr>' +
      '<th>游戏</th><th>日常</th><th>体力</th><th>进度</th></tr></thead><tbody>' +
      games.map(function(g){
        const name = escHtml(g.game_name || g.game);
        if (!g.ok) {
          return '<tr><td>' + name + '</td>' +
            '<td><span class="badge err">失败</span></td>' +
            '<td class="wrap err-t">' + escHtml(g.error || '未知错误') + '</td>' +
            '<td>—</td></tr>';
        }
        const r = g.resource;
        const st = g.finished
          ? '<span class="dim">已完成</span>'
          : '<span class="badge warn">未完成</span>';
        if (!r || r.current == null) {
          return '<tr><td>' + name + '</td><td>' + st + '</td>' +
            '<td class="dim">—</td><td class="dim">暂不支持</td></tr>';
        }
        const pct = (r.percent == null) ? 0 : r.percent;
        const max = (r.maximum == null) ? '?' : r.maximum;
        return '<tr><td>' + name + '</td><td>' + st + '</td>' +
          '<td class="mono">' + r.current + ' <span class="dim">/ ' + max +
          ' · ' + escHtml(r.name) + '</span></td>' +
          '<td><span class="bar"><i class="' + (pct >= 85 ? 'hot' : '') +
          '" style="width:' + pct + '%"></i><i class="progress-glow' +
          (pct >= 85 ? ' hot' : '') + '" style="left:' + pct + '%"></i></span><span class="dim">' + pct + '%</span></td></tr>';
      }).join('') + '</tbody></table>';
  }
  if (hint) {
    hint.textContent = '查询时间 ' + (d.generated_at || '-') + (d.cached ? '（45 秒内缓存）' : '');
  }
  // 迷你卡的体力行用最新数据重绘（报表页当前可见时才有意义）
  const statsBox = document.getElementById('h-stats');
  if (lastHistory && lastHistory.has_data && statsBox && statsBox.querySelector('.gcard')) {
    statsBox.innerHTML = '<div class="gcards">' +
      (lastHistory.games || []).map(gameCardHtml).join('') + '</div>';
  }
  renderKpiResin(d);
  /* 实时结果一到手就重算「今日完成」KPI：live 说已完成的立刻计入，
     不等下一次巡检落盘 —— KPI 与表格永远对得上 */
  if (lastHistory) renderKpiToday(lastHistory);
}

function loadLive(force){
  const btn = document.getElementById('btn-live-refresh');
  const box = document.getElementById('live-grid');
  if (btn) { btn.disabled = true; btn.textContent = '查询中…'; }
  if (box) box.innerHTML = '<div class="emptybox">正在查询，请稍候…</div>';
  fetch('/api/live' + (force ? '?refresh=1' : ''))
    .then(function(r){ return r.json(); })
    .then(function(d){ liveLoaded = true; renderLive(d); })
    .catch(function(){
      if (box) box.innerHTML = '<div class="emptybox">查询失败，请稍后重试。</div>';
    })
    .then(function(){
      if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
    });
}

function renderActHint(d){
  const hint = document.getElementById('act-hint');
  if (!hint) return;
  d = d || {};
  const parts = [];
  if (d.last_error) {
    parts.push('上次拉取失败（沿用旧数据）：' + d.last_error);
  } else if (d.fetched_at) {
    parts.push('数据更新于 ' + d.fetched_at);
  }
  if (d.ignored_count > 0) {
    parts.push('已删除 ' + d.ignored_count + ' 个');
  }
  hint.innerHTML = escHtml(parts.join(' · ')) +
    (d.ignored_count > 0 ? ' <a class="act-restore" id="act-restore">恢复全部</a>' : '');
  const restore = document.getElementById('act-restore');
  if (restore) restore.addEventListener('click', restoreActivities);
}

/* 游戏头像：按 game_id 硬绑定官方图标。
   ICON_STAMP 由后端按图片 mtime 注入，换图后 URL 必变，不会被 WebView2 缓存坑。 */
const ICON_STAMP = '__ICON_STAMP__';
const GAME_ICON = {
  genshin: '/static/icons/genshin',
  starrail: '/static/icons/starrail',
  zzz: '/static/icons/zzz',
  arknights: '/static/icons/arknights'
};

/* 图标定义就绪后，把账号绑定页选择卡的静态占位换成官方图标 */
fillSelIcons();

function actIconHtml(it){
  const base = GAME_ICON[it.game];
  if (!base) {
    return '<span class="act-icon ph">' +
      escHtml(String(it.game_name || '?').slice(0, 1)) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="act-icon" width="24" height="24" alt="' +
    escHtml(it.game_name || '') + '" src="' + base + '.png' + v +
    '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

/* '2026-10-29 04:00:00' -> '10-29 04:00'（卡片头部精确到分） */
function shortRange(s){
  const m = String(s || '').match(/\d{4}-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/);
  return m ? m[1] + '-' + m[2] + ' ' + m[3] : '';
}

/* 最紧急 1 张的紧迫度标记（只改颜色不改尺寸）：剩余 ≤24h = critical（红边+呼吸）；≤72h（ending_soon）= warn（琥珀数字） */
function actUrgencyAttr(it, urgent){
  if (!urgent || it.not_started) return '';
  const hl = +it.hours_left || 0;
  if (it.ending_soon && hl > 0 && hl <= 24) return ' data-urgency="critical"';
  if (it.ending_soon) return ' data-urgency="warn"';
  return '';
}

function actCardHtml(it, urgent){
  const id = escHtml(it.id || '');
  const st = shortRange(it.start), en = shortRange(it.end);
  const ended = it.status === '已完成';
  const timeCls = ended ? 't-end' : (it.not_started ? 't-up' : 't-on');
  const soon = !!it.ending_soon && !it.not_started;
  // reward 为空 = 接口没给奖励明细，被兜底规则保留下来的（别假装它有原石）
  const reward = it.reward
    ? '<span class="act-reward" title="这个活动给抽卡资源">' + escHtml(it.reward) + '</span>'
    : '';
  const cdCls = ended ? 'ended' : (soon ? 'soon' : '');
  const prog = actProgHtml(it);
  return '<div class="card acard"' +
    actUrgencyAttr(it, urgent) + ' data-id="' + id + '">' +
    '<div class="acard-top"><span class="act-game">' + actIconHtml(it) +
      escHtml(it.game_name) + '</span>' +
      '<button type="button" class="act-del" data-del="' + id +
      '" title="删除，之后不再提醒">×</button></div>' +
    '<div class="acard-title" title="' + escHtml(it.title) + '">' + escHtml(it.title) + reward + '</div>' +
    '<div class="act-time ' + timeCls + '">始 ' + (st || '时间待定') +
      (en ? ' · 止 ' + en : '') + '</div>' +
    '<div class="act-cd ' + cdCls + '">' + escHtml(it.countdown || '') + '</div>' +
    prog +
    '</div>';
}

/* 36/36 这类文本进度 -> 进度条；解析不出就不画 */
function actProgHtml(it){
  const m = String(it.progress || '').match(/(\d+)\s*\/\s*(\d+)/);
  if (!m) return '';
  const a = +m[1], b = +m[2];
  if (!b) return '';
  const pct = Math.min(100, Math.round(a / b * 100));
  return '<div class="act-prog"><span class="sbar"><i style="width:' + pct + '%"></i></span>' +
    '<span class="mono">' + a + '/' + b + '</span></div>';
}

/* 找最紧急的 1 张（剩余小时最小且 >0；没有进行中就取未开启第一张），只用于 data-urgency 标记 */
function pickHeroIndex(pool){
  if (!pool.length) return -1;
  let best = 0;
  for (let i = 1; i < pool.length; i++) {
    const a = +pool[i].hours_left || 0, b = +pool[best].hours_left || 0;
    if (a > 0 && (b <= 0 || a < b)) best = i;
  }
  return best;
}

function renderActivities(d){
  const list = document.getElementById('act-list');
  const uns = document.getElementById('act-unsupported');
  if (!list) return;
  d = d || {};
  const items = d.items || [];
  if (!items.length) {
    list.innerHTML = '<div class="act-empty">' +
      '<div class="act-empty-icon">' + (d.has_data ? '🌱' : '📭') + '</div>' +
      '<div>' + (d.has_data
        ? '近期无即将结束的活动，安心长草'
        : '还没有活动数据，点「刷新」拉取。') + '</div>' +
      '</div>';
  } else {
    // 后端已排好序；这里拆成 进行中 / 未开启 / 已完成（进抽屉）三段
    const ongoing = items.filter(function(x){ return x.group !== 'upcoming' && x.status !== '已完成'; });
    const upcoming = items.filter(function(x){ return x.group === 'upcoming'; });
    const finished = items.filter(function(x){ return x.status === '已完成'; });

    let html = '<div class="act-grid">';
    if (ongoing.length) {
      const urgentIdx = pickHeroIndex(ongoing);
      html += ongoing.map(function(it, i){ return actCardHtml(it, i === urgentIdx); }).join('');
    }
    if (upcoming.length) {
      html += upcoming.map(function(it){ return actCardHtml(it, !ongoing.length && it === upcoming[0]); }).join('');
    }
    html += '</div>';

    if (finished.length) {
      html += '<div class="act-drawer" id="act-drawer">' +
        '<button type="button" class="act-drawer-handle">已完成 · ' + finished.length + '</button>' +
        '<div class="act-drawer-body"><div class="act-grid">' +
        finished.map(function(it){ return actCardHtml(it, false); }).join('') +
        '</div></div></div>';
    }
    list.innerHTML = html;
  }
  renderActHint(d);
  if (uns) {
    const list2 = d.unsupported || [];
    uns.innerHTML = list2.length
      ? '<div class="au-title">暂不支持</div>' +
        list2.map(function(u){
          return '<span class="au-chip" title="' + escHtml(u.reason || '') + '">' +
            escHtml(u.game_name) + '</span>';
        }).join('')
      : '';
  }
}

/* 删除：立刻从界面消失，并写进 data/ignored_activities.json，之后永不提醒 */
async function deleteActivity(btn){
  const id = btn.getAttribute('data-del');
  const row = btn.closest('.acard');
  btn.disabled = true;
  try {
    const d = await postJSON('/api/activities/delete', { id: id });
    if (!d || d.ok === false) {
      toast((d && d.message) || '删除失败', true);
      btn.disabled = false;
      return;
    }
    if (row) row.remove();
    renderActivities(d);
    toast('已删除，之后不会再提醒');
  } catch (e) {
    btn.disabled = false;
    toast('删除失败：' + e, true);
  }
}

async function restoreActivities(){
  try {
    const d = await postJSON('/api/activities/restore', {});
    if (!d || d.ok === false) { toast('恢复失败', true); return; }
    renderActivities(d);
    toast('已恢复全部被删除的活动');
  } catch (e) {
    toast('恢复失败：' + e, true);
  }
}

function loadActivities(){
  fetch('/api/activities').then(function(r){ return r.json(); })
    .then(renderActivities)
    .catch(function(){
      const list = document.getElementById('act-list');
      if (list) list.innerHTML = '<div class="emptybox">读取活动缓存失败，请点「刷新」。</div>';
    });
}

async function refreshActivities(){
  const btn = document.getElementById('btn-act-refresh');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '刷新中…';
  try {
    const d = await postJSON('/api/activities/refresh', {});
    if (d.ok === false) { toast(d.message || '刷新失败', true); return; }
    renderActivities(d);
    toast('活动数据已更新（' + (d.items || []).length + ' 个进行中）');
  } catch (e) {
    toast('刷新失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

/* =========================== 每日报表 =========================== */
function renderHistoryEmpty(message, detail){
  // 空状态：统计表清空，表格位置放一句人话（绝不弹红色错误条）
  const statsBox = document.getElementById('h-stats');
  if (statsBox) statsBox.innerHTML = '';
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = '-';
  const tableBox = document.getElementById('h-table');
  if (!tableBox) return;
  tableBox.innerHTML =
    '<div class="emptybox">' + message +
    (detail ? '<br><span style="font-size:11px;color:var(--muted)">' + detail + '</span>' : '') +
    '</div>';
}

function gameBaseName(g){
  // 只取游戏基础名：去掉「 · 角色昵称」。
  var raw = String((g && (g.game_name || g.game)) || '');
  var cut = raw.indexOf('·');
  if (cut > 0) raw = raw.slice(0, cut);
  return raw.trim() || String((g && g.game) || '');
}

// 游戏识别色（BetterGI 式图标占位小圆点）
const GAME_DOT = {
  genshin:   '#e0a83c',
  starrail:  '#8f7ff0',
  zzz:       '#f0854b',
  arknights: '#4aa3e8',
  endfield:  '#dcc23c'
};
function gameDotColor(gid){
  return GAME_DOT[gid] || 'var(--brand)';
}

let lastHistory = null;

function renderHistory(d){
  d = d || {};
  lastHistory = d;
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = d.month ? (d.month + ' 本月') : '-';

  if (!d.has_data) {
    renderHistoryEmpty('暂无历史记录，等待下一次定时检查写入。');
    return;
  }
  const games = d.games || [];
  if (!games.length) {
    renderHistoryEmpty('没有启用任何游戏账号。');
    return;
  }

  // --- 游戏概览：每款一张迷你卡 ---
  const statsBox = document.getElementById('h-stats');
  if (statsBox) {
    statsBox.innerHTML = '<div class="gcards">' +
      games.map(gameCardHtml).join('') + '</div>';
  }
  renderHeatmap(d);
}

function gameIconHtml(gid){
  const base = GAME_ICON[gid];
  if (!base) {
    return '<span class="gcard-ico ph">' + escHtml(String(gid).slice(0, 1).toUpperCase()) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="gcard-ico" width="40" height="40" alt="" src="' + base + '.png' + v +
         '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

function todayCellFor(d, gid){
  const days = (d && d.days) || [];
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) return (days[i].games || {})[gid] || null;
  }
  return null;
}

function gameCardHtml(g){
  const gid = g.game;
  const today = todayCellFor(lastHistory, gid);
  const live = (window.__liveMap || {})[gid];

  // 今日状态：live 查询成功以 live 为准（与 KPI/表格同一套判定），
  // 没有实时数据时回落巡检历史。正常 = 灰字，异常 = 橙色徽章（色彩纪律）
  let todayHtml;
  if (live && live.finished != null) {
    todayHtml = live.finished ? '<span class="dim">✔ 今日已完成</span>'
                              : '<span class="badge warn">未完成</span>';
  } else if (!today || !today.known) {
    todayHtml = '<span class="dim">今日无数据</span>';
  } else if (today.finished) {
    todayHtml = '<span class="dim">✔ 今日已完成</span>';
  } else {
    todayHtml = '<span class="badge warn">未完成 ' + (today.unfinished_tasks || 0) + ' 项</span>';
  }

  // 体力：来自「信息查询」最近一次结果（未查过则提示）
  let stamina;
  if (live && live.percent != null) {
    const hot = live.percent >= 85;
    stamina = '<div class="gcard-stam"><span class="dim">' + escHtml(live.name || '体力') +
      '</span><span class="sbar"><i class="' + (hot ? 'hot' : '') +
      '" style="width:' + live.percent + '%"></i><i class="progress-glow' +
      (hot ? ' hot' : '') + '" style="left:' + live.percent + '%"></i></span>' +
      '<span class="mono">' + live.current + '/' + live.maximum + '</span></div>';
  } else {
    stamina = '<div class="gcard-stam"><span class="dim">体力</span>' +
      '<span class="dim">到「信息查询」查询后显示</span></div>';
  }

  const missed = Number(g.month_missed || 0);
  return '<div class="card gcard">' +
    '<div class="gcard-head">' + gameIconHtml(gid) +
      '<div class="gcard-name"><b>' + escHtml(gameBaseName(g)) + '</b>' +
        '<small>' + (today && today.last ? escHtml(shortStamp(today.last)) : '—') + '</small></div>' +
      '<div class="gcard-streak"><b class="value">' + Number(g.streak || 0) + '</b><small>连续天数</small></div>' +
    '</div>' +
    '<div class="gcard-today">' + todayHtml + '</div>' +
    /* 进度透明化：直接展示每条判定任务的进度（live 优先） */
    taskProgressHtml(live && live.tasks) +
    stamina +
    '<div class="gcard-foot">本月完成 <b>' + Number(g.month_done || 0) + '</b> 天' +
      (missed > 0 ? ' · <span class="err-t">漏 ' + missed + '</span>' : '') +
    '</div></div>';
}

/* GitHub 式热力图：行 = 游戏，列 = 日期；绿=完成 红=未完成 灰=无数据 */
function renderHeatmap(d){
  const box = document.getElementById('h-table');
  if (!box) return;
  const days = d.days || [];
  const tracked = d.tracked || [];
  if (!tracked.length) {
    box.innerHTML = '<div class="emptybox">没有启用任何游戏账号。</div>';
    return;
  }
  const nameOf = {};
  (d.games || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });

  let html = '<div class="heatmap">';
  html += '<div class="hm-row hm-head"><div class="hm-label"></div>' +
    days.map(function(row){
      return '<div class="hm-cell' + (row.is_today ? ' today' : '') + '">' +
        escHtml(String(row.label || '').replace(/^\d{4}-/, '')) + '</div>';
    }).join('') + '</div>';

  tracked.forEach(function(gid){
    html += '<div class="hm-row"><div class="hm-label">' +
      '<span class="gdot" style="background:' + gameDotColor(gid) + '"></span>' +
      escHtml(gameBaseName({ game: gid, game_name: nameOf[gid] })) + '</div>';
    days.forEach(function(row){
      const c = (row.games || {})[gid];
      let cls = 'na', mark = '';
      if (c && c.known) {
        cls = c.finished ? 'ok' : 'err';
        mark = c.finished ? '✓' : '✗';
      } else if (row.is_today) {
        /* 今天的定时巡检还没到：画"等待中"灰点，绝不提前画红叉 */
        cls = 'na wait';
        mark = '⋯';
      }
      const tip = (c && c.known)
        ? (row.label + ' · ' + (c.finished ? '已完成'
            : '未完成 ' + (c.unfinished_tasks || 0) + ' 项'))
        : (row.is_today ? (row.label + ' · 等待今日巡检')
                        : (row.label + ' · 无数据'));
      html += '<div class="hm-cell ' + cls + (row.is_today ? ' today' : '') +
        '" title="' + escHtml(tip) + '">' + mark + '</div>';
    });
    html += '</div>';
  });

  html += '</div>' +
    '<div class="hm-legend">' +
    '<span class="hm-cell ok">✓</span>完成' +
    '<span class="hm-cell err">✗</span>未完成' +
    '<span class="hm-cell na">⋯</span>等待中/无数据' +
    '</div>';
  box.innerHTML = html;
}

function loadHistory(){
  fetch('/api/history?days=7')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(d){
      try {
        renderHistory(d);
        renderKpiToday(d);
      } catch (e) {
        console.error('渲染历史报表失败', e);
        renderHistoryEmpty('报表渲染出错，请点「刷新」重试。');
      }
    })
    .catch(function(err){
      console.error('读取历史记录失败', err);
      renderHistoryEmpty('暂无历史记录，等待下一次定时检查写入。',
                         err && err.message ? '（' + err.message + '）' : '');
    });
}

document.getElementById('btn-history-refresh').addEventListener('click', loadHistory);

/* ========================= 主题切换 ========================= */
/* auto = 跟随系统，light / dark = 手动固定；选择存在 localStorage，重启后保留。
   CSS 变量只认 html[data-theme="dark"|"light"] 两组 Token；
   auto 在这里解析成系统实际值后同样落到 data-theme 上，杜绝两边逻辑分叉。 */
const THEME_ORDER = ['auto', 'light', 'dark'];
const THEME_NAMES = { auto: '跟随系统', light: '浅色', dark: '深色' };
let themeCur = 'auto';
try { themeCur = localStorage.getItem('rl-theme') || 'auto'; } catch (e) {}
if (THEME_ORDER.indexOf(themeCur) < 0) themeCur = 'auto';

const systemDark = window.matchMedia('(prefers-color-scheme: dark)');

function applyTheme(){
  const mode = themeCur === 'auto' ? (systemDark.matches ? 'dark' : 'light') : themeCur;
  document.documentElement.dataset.theme = mode;
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = '主题：' + THEME_NAMES[themeCur];
}

applyTheme();
document.getElementById('theme-btn').addEventListener('click', function(){
  themeCur = THEME_ORDER[(THEME_ORDER.indexOf(themeCur) + 1) % THEME_ORDER.length];
  try { localStorage.setItem('rl-theme', themeCur); } catch (e) {}
  applyTheme();
});
/* 跟随系统时，系统深浅变化实时切换 */
if (systemDark.addEventListener) {
  systemDark.addEventListener('change', function(){ if (themeCur === 'auto') applyTheme(); });
}

/* ========================= 推送预览 ========================= */
function renderPushPreview(h){
  const titleEl = document.getElementById('pv-title');
  const bodyEl = document.getElementById('pv-body');
  const chEl = document.getElementById('pv-channel');
  if (!titleEl || !bodyEl) return;

  const channels = [];
  const sc = document.getElementById('serverchan-enabled');
  const qm = document.getElementById('qmsg-enabled');
  if (sc && sc.checked) channels.push('Server酱');
  if (qm && qm.checked) channels.push('Qmsg 酱');
  if (chEl) chEl.textContent = channels.length
    ? ('推送到 ' + channels.join(' + ')) : '未启用推送渠道';

  const days = (h && h.days) || [];
  let row = null;
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) { row = days[i]; break; }
  }
  if (!row && days.length) row = days[days.length - 1];

  if (!row) {
    titleEl.textContent = '游戏日常：等待第一次巡检';
    bodyEl.textContent = '还没有巡检数据。定时检查跑过一轮后，这里会按真实结果显示推送样式。';
    return;
  }

  const cells = row.games || {};
  const nameOf = {};
  ((h && h.games) || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });
  const tracked = (h && h.tracked) || [];
  const done = [], miss = [];
  let lastTime = '';
  tracked.forEach(function(gid){
    const c = cells[gid];
    if (!c || !c.known) return;
    if (c.last && c.last > lastTime) lastTime = c.last;
    if (c.finished) done.push(nameOf[gid] || gid);
    else miss.push((nameOf[gid] || gid) +
      (c.unfinished_tasks ? '（漏 ' + c.unfinished_tasks + ' 项）' : ''));
  });
  const total = done.length + miss.length;
  const allDone = total > 0 && !miss.length;
  titleEl.textContent = allDone
    ? ('游戏日常：' + total + '/' + total + ' 全部完成')
    : ('游戏日常：' + miss.length + '/' + (total || tracked.length) + ' 个未完成');

  const lines = [];
  lines.push((row.label || '') + (row.weekday ? ' ' + row.weekday : '') +
             (lastTime ? ' · ' + lastTime : ''));
  miss.forEach(function(m){ lines.push('[未完成] ' + m); });
  if (done.length) lines.push('[已完成] ' + done.join('、'));
  lines.push(allDone ? '今天的日常全部完成，可以放心摆烂了。'
                     : '别拖到 23:59，记得把日常清了。');
  bodyEl.textContent = lines.join('\n');
}

function loadPushPreview(){
  fetch('/api/history?days=1').then(function(r){ return r.json(); })
    .then(renderPushPreview)
    .catch(function(){
      const t = document.getElementById('pv-title');
      if (t) t.textContent = '游戏日常：预览不可用';
      const b = document.getElementById('pv-body');
      if (b) b.textContent = '读取巡检数据失败，请稍后重试。';
    });
}

/* ========================= 今日巡检动态 ========================= */
function renderRunLog(h){
  const box = document.getElementById('run-log');
  if (!box) return;
  const days = (h && h.days) || [];
  const nameOf = {};
  ((h && h.games) || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });
  const tracked = (h && h.tracked) || [];
  const rows = days.filter(function(row){
    const cells = row.games || {};
    return Object.keys(cells).some(function(k){ return cells[k] && cells[k].known; });
  }).slice(-3).reverse();

  if (!rows.length) {
    box.innerHTML = '<div class="emptybox">还没有巡检记录，等第一次定时检查跑完后这里会有动态。</div>';
    return;
  }
  box.innerHTML = rows.map(function(row){
    const cells = row.games || {};
    const done = [], miss = [];
    let lastTime = '';
    tracked.forEach(function(gid){
      const c = cells[gid];
      if (!c || !c.known) return;
      if (c.last && c.last > lastTime) lastTime = c.last;
      if (c.finished) done.push(nameOf[gid] || gid);
      else miss.push(nameOf[gid] || gid);
    });
    const summary = miss.length
      ? '<span class="pill no">' + miss.length + ' 款未完成</span>'
      : '<span class="dim">全部完成</span>';
    const detail = miss.length
      ? '<span class="log-detail">未完成：' + escHtml(miss.join('、')) + '</span>' : '';
    return '<div class="log-row">' +
      '<span class="log-date">' + escHtml(row.label || '') +
        ' <span class="dim">' + escHtml(row.weekday || '') +
        (row.is_today ? ' · 今天' : '') + '</span></span>' +
      '<span class="log-time">' + escHtml(lastTime || '--:--') + '</span>' +
      summary + detail +
      '</div>';
  }).join('');
}

function loadRunLog(){
  fetch('/api/history?days=7').then(function(r){ return r.json(); })
    .then(renderRunLog)
    .catch(function(){
      const box = document.getElementById('run-log');
      if (box) box.innerHTML = '<div class="emptybox">读取巡检记录失败，请点「刷新」重试。</div>';
    });
}

document.getElementById('btn-log-refresh')
  .addEventListener('click', loadRunLog);

/* ========================= 全局 Tooltip ========================= */
const tipBox = document.createElement('div');
tipBox.id = 'tip';
document.body.appendChild(tipBox);

document.addEventListener('mouseover', function(e){
  const el = e.target.closest ? e.target.closest('[data-tip]') : null;
  if (!el) return;
  const text = el.getAttribute('data-tip') || '';
  if (!text) return;
  tipBox.textContent = text;
  tipBox.classList.add('on');          // 先显示才能量出气泡尺寸
  const r = el.getBoundingClientRect();
  const tw = tipBox.offsetWidth, th = tipBox.offsetHeight;
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
  let y = r.top - th - 8;              // 默认在图标上方，贴顶时翻到下方
  if (y < 8) y = r.bottom + 8;
  tipBox.style.left = Math.round(x) + 'px';
  tipBox.style.top = Math.round(y) + 'px';
});
document.addEventListener('mouseout', function(e){
  if (e.target.closest && e.target.closest('[data-tip]')) tipBox.classList.remove('on');
});
window.addEventListener('scroll', function(){ tipBox.classList.remove('on'); }, true);

/* ========================= KPI 摘要条 ========================= */
function kpiSet(id, text){
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* 紧迫感系统：把 warn/critical 写到 KPI 卡片的 data-urgency 上 */
function setUrgency(id, level){
  const el = document.getElementById(id);
  if (!el) return;
  const card = el.closest('.kpi');
  if (card) card.dataset.urgency = level;
}

/* KPI 智能吸顶：滚动容器是 .main（不是 window） */
const kpiBarEl = document.getElementById('kpi-bar');
const kpiMainEl = document.querySelector('.main');

function kpiUrgencyScore(card){
  const u = card.dataset.urgency;
  return u === 'critical' ? 2 : (u === 'warn' ? 1 : 0);
}

/* compact 态只保留最紧急的 2 个信号（不足 2 个紧急时用普通卡补位） */
function refreshKpiKeep(){
  if (!kpiBarEl) return;
  const cards = Array.prototype.slice.call(kpiBarEl.querySelectorAll('.kpi'));
  const sorted = cards.slice().sort(function(a, b){
    return kpiUrgencyScore(b) - kpiUrgencyScore(a);
  });
  sorted.forEach(function(c, i){ c.classList.toggle('keep', i < 2); });
}

function updateKpiCompact(){
  if (!kpiBarEl || !kpiMainEl) return;
  kpiBarEl.classList.toggle('compact', kpiMainEl.scrollTop > 80);
}
if (kpiMainEl) kpiMainEl.addEventListener('scroll', updateKpiCompact, {passive:true});
updateKpiCompact();

/* 今日完成：巡检历史 + 实时查询合并统计。
   数据源纪律：/api/live 查询成功的游戏以 live 为准（现场状态最权威）；
   live 没查到/查询失败的回落 /api/history 的粘性完成。两路永远不会打架。 */
function renderKpiToday(h){
  const el = document.getElementById('k-today');
  if (!el) return;
  const days = (h && h.days) || [];
  let row = null;
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) { row = days[i]; break; }
  }
  const tracked = (h && h.tracked) || [];
  const cells = (row && row.games) || {};
  const liveMap = window.__liveMap || {};
  let done = 0, known = 0;
  tracked.forEach(function(gid){
    const lv = liveMap[gid];
    if (lv && lv.finished != null) {      // live 说了算
      known++;
      if (lv.finished) done++;
      return;
    }
    const c = cells[gid];
    if (!c || !c.known) return;           // 两路都没有 → 不计入
    known++;
    if (c.finished) done++;
  });
  const total = tracked.length;
  if (!total) {
    el.textContent = '–';
    el.className = 'kpi-value value';
    setUrgency('k-today', '');
    kpiSet('k-today-sub', '没有启用任何游戏');
    return;
  }
  el.textContent = done + '/' + total;
  el.className = 'kpi-value value' + (known && done !== total ? ' warn' : '');
  /* 0 完成 = critical；部分完成 = warn；全部完成 = 无紧迫（紧迫度只看合并结果，不碰统计） */
  setUrgency('k-today', !known ? '' : (done === total ? '' : (done > 0 ? 'warn' : 'critical')));
  refreshKpiKeep();
  kpiSet('k-today-sub', !known ? '今日还没检查过，查询中…'
    : (done === total ? '今日全部完成' : (total - done) + ' 款未完成'));
}

/* 凭证健康：已绑定凭证的账号 / 启用账号 */
function renderKpiCred(d){
  const el = document.getElementById('k-cred');
  if (!el) return;
  const items = (d && d.items) || [];
  if (!items.length) {
    el.textContent = '–';
    el.className = 'kpi-value value';
    setUrgency('k-cred', '');
    kpiSet('k-cred-sub', '没有账号');
    return;
  }
  const bound = items.filter(function(i){ return i.has_credential; }).length;
  el.textContent = bound + '/' + items.length;
  el.className = 'kpi-value value' + (bound === items.length ? '' : ' warn');
  setUrgency('k-cred', bound === items.length ? '' : 'warn');
  refreshKpiKeep();
  kpiSet('k-cred-sub', bound === items.length ? '全部已绑定'
    : (items.length - bound) + ' 个未绑定');
}

/* 下次巡查：取 schedule 的 next_run，展示时刻 + 每 30 秒刷新倒计时 */
let kpiNext = null;

function renderKpiNext(d){
  const el = document.getElementById('k-next');
  if (!el) return;
  const raw = String((d && d.next_run) || '');
  const m = raw.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/);
  if (m) {
    kpiNext = new Date(+m[1], +m[2] - 1, +m[3], +m[4].slice(0, 2), +m[4].slice(3, 5));
    el.textContent = m[4];
    updateKpiCountdown();
  } else {
        kpiNext = null;
    el.textContent = raw || '–';
    el.className = 'kpi-value value';
    kpiSet('k-next-sub', raw ? '' : '守护进程未运行');  }
}

function updateKpiCountdown(){
  const sub = document.getElementById('k-next-sub');
  if (!sub) return;
  if (!kpiNext) { sub.textContent = '守护进程未运行'; return; }
  const diff = Math.max(0, kpiNext.getTime() - Date.now());
  const min = Math.floor(diff / 60000);
  const h = Math.floor(min / 60), m = min % 60;
  sub.textContent = h > 0 ? (h + ' 小时 ' + m + ' 分后')
    : (m > 0 ? m + ' 分钟后' : '1 分钟内');
}
setInterval(updateKpiCountdown, 30000);

/* 体力临满：最近一次「信息查询」结果里 ≥85% 的游戏数 */
function renderKpiResin(d){
  const el = document.getElementById('k-resin');
  if (!el) return;
  if (!d || d.ok === false) return;
  const games = d.games || [];
  let hot = 0, withRes = 0;
  games.forEach(function(g){
    if (!g.ok || !g.resource || g.resource.percent == null) return;
    withRes++;
    if (g.resource.percent >= 85) hot++;
  });
  el.textContent = withRes ? String(hot) : '–';
  el.className = 'kpi-value value' + (hot ? ' warn' : '');
  setUrgency('k-resin', hot > 0 ? 'warn' : '');
  refreshKpiKeep();
  kpiSet('k-resin-sub', withRes
    ? (hot ? hot + ' 款即将溢出' : '体力都还充裕')
    : '到「信息查询」查询后显示');
}

/* ========================= 24 小时标尺 ========================= */
function renderRuler(times){
  const box = document.getElementById('d-ruler');
  if (!box) return;
  let html = '<div class="ruler-track"></div>';
  [0, 6, 12, 18, 24].forEach(function(h){
    html += '<div class="ruler-tick" style="left:' + (h / 24 * 100) + '%">' +
      (h < 10 ? '0' + h : h) + ':00</div>';
  });
  if (!times.length) {
    html += '<div class="ruler-empty">还没有时间点，在下面添加</div>';
  }
  times.forEach(function(t){
    const m = String(t).match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return;
    const pct = ((+m[1] * 60 + +m[2]) / 1440 * 100).toFixed(3);
    html += '<div class="ruler-dot" style="left:' + pct + '%" title="' + t + '">' +
            '<span>' + t + '</span></div>';
  });
  box.innerHTML = html;
}

/* ========================= 底部日志控制台 ========================= */
let consoleLoaded = false;

function renderConsole(d){
  const body = document.getElementById('console-body');
  const last = document.getElementById('console-last');
  if (!body) return;
  if (!d || d.ok === false) {
    body.innerHTML = '<span class="console-empty">' +
      escHtml((d && d.message) || '读取失败，请点「刷新」重试。') + '</span>';
    return;
  }
  const lines = d.lines || [];
  if (!lines.length) {
    body.innerHTML = '<span class="console-empty">还没有日志。</span>';
    if (last) last.textContent = '暂无日志';
    return;
  }
  body.innerHTML = lines.map(function(line){
    let cls = 'cline';
    if (/ERROR|CRITICAL/.test(line)) cls += ' c-err';
    else if (/WARNING/.test(line)) cls += ' c-warn';
    else if (/推送成功/.test(line)) cls += ' c-ok';
    const marked = escHtml(line).replace(
      /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/, '<span class="c-time">$1</span>');
    return '<div class="' + cls + '">' + marked + '</div>';
  }).join('');
  body.scrollTop = body.scrollHeight;
  if (last) last.textContent = lines[lines.length - 1].replace(/^\S+\s+/, '');
}

function loadConsole(){
  fetch('/api/logtail?n=200').then(function(r){ return r.json(); })
    .then(function(d){ consoleLoaded = true; renderConsole(d); })
    .catch(function(){
      const body = document.getElementById('console-body');
      if (body) body.innerHTML = '<span class="console-empty">读取日志失败，请点「刷新」。</span>';
    });
}

document.getElementById('log-console').addEventListener('toggle', function(){
  if (this.open && !consoleLoaded) loadConsole();
});
document.getElementById('btn-console-refresh').addEventListener('click', function(e){
  // 阻止 summary 的默认展开/收起行为，只刷新
  e.preventDefault();
  e.stopPropagation();
  loadConsole();
});

/* =========================== 启动 =========================== */
const hash = (location.hash || '').replace('#', '');
switchTab(PANEL_NAMES.indexOf(hash) >= 0 ? hash : 'history');
refreshNotice();
loadDevice();
loadAccounts();      // 用 config 里的真实巡检开关覆盖页面默认勾选
loadThresholds();
loadSchedule();      // 状态先拉一次，展开时不会闪空
loadAutostart();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyCheckLogin/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, path: str) -> None:
        """只吐 static/ 下的图片，别的路径一律 404（顺手挡住 ../ 穿越）。"""
        rel = path[len("/static/"):]
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = STATIC_TYPES.get(target.suffix.lower())
        if ctype is None or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        # 不缓存：图标是本地文件，重下一遍的成本远低于「换了图还看旧图」的排查成本
        self._send(200, body, ctype, cache="no-store")

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._send(200, _render_page().encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/start":
            provider = (query.get("provider") or [""])[0]
            raw = (query.get("targets") or [""])[0]
            targets = [t.strip() for t in raw.split(",") if t.strip()]

            if provider == "mihoyo":
                bad = [t for t in targets if t not in MIHOYO_GAMES]
                if not targets or bad:
                    self._json(
                        {"ok": False, "message": f"无效的游戏参数：{', '.join(bad) or '空'}"},
                        400,
                    )
                    return
                session = Session("mihoyo", targets)
                _put(session)
                threading.Thread(target=run_mihoyo, args=(session,), daemon=True).start()
                self._json({"ok": True, "sid": session.sid, "labels": session.labels()})
                return

            if provider == "skland":
                bad = [t for t in targets if t not in SKLAND_TARGETS]
                if not targets or bad:
                    self._json(
                        {"ok": False, "message": f"无效的游戏参数：{', '.join(bad) or '空'}"},
                        400,
                    )
                    return
                session = Session("skland", targets)
                _put(session)
                threading.Thread(target=run_skland, args=(session,), daemon=True).start()
                self._json({"ok": True, "sid": session.sid, "labels": session.labels()})
                return

            self._json({"ok": False, "message": "未知的 provider"}, 400)
            return

        if parsed.path == "/api/accounts":
            self._json(accounts_status())
            return

        if parsed.path == "/api/env":
            self._json(push_status())
            return

        if parsed.path == "/api/push":
            # 只返回掩码，明文 Key 永远不出后端
            self._json(push_detail())
            return

        if parsed.path == "/api/device":
            self._json(device_status())
            return

        if parsed.path == "/api/schedule":
            self._json(schedule_status())
            return

        if parsed.path == "/api/history":
            try:
                days = int((query.get("days") or ["7"])[0])
            except (TypeError, ValueError):
                days = 7
            from core import history

            # 列 = 当前启用账号 ∪ 历史里出现过的游戏：刚启用、还没跑过一轮的
            # 游戏也会立刻出现在报表里（显示为 –），不会被当成"从未存在"。
            enabled_games = _enabled_game_ids()

            # 任何异常都必须变成"200 + 空报表"，绝不能 500：
            # 前端 fetch 拿到 500 时 r.json() 会失败，弹的是一句让人摸不着头脑的
            # "无法读取历史记录"红字，而真实原因只是 data/history.json 还没生成。
            try:
                payload = history.summary(days=days, enabled_games=enabled_games)
            except Exception:  # noqa: BLE001
                logger.exception("读取历史记录失败，返回空报表")
                payload = history.empty_payload(days=days)
                payload["message"] = "读取历史记录失败，已显示空报表（详见 logs/dailycheck.log）"
            self._json(payload)
            return

        if parsed.path == "/api/state":
            sid = (query.get("sid") or [""])[0]
            session = _get(sid)
            if session is None:
                self._json({"state": "error", "message": "会话已失效，请重新开始"}, 404)
                return
            self._json(session.snapshot())
            return

        if parsed.path == "/api/logtail":
            try:
                n = int((query.get("n") or ["200"])[0])
            except (TypeError, ValueError):
                n = 200
            self._json(log_tail(max(10, min(n, 500))))
            return

        if parsed.path == "/api/settings":
            self._json(thresholds_status())
            return

        if parsed.path == "/api/autostart":
            from core import autostart

            self._json(autostart.status())
            return

        if parsed.path == "/api/activities":
            from core import activities

            try:
                self._json(activities.summary())
            except Exception as exc:  # noqa: BLE001 - 页面读不到也要给人话
                logger.exception("读取活动缓存失败")
                self._json({"ok": False, "message": f"读取活动缓存失败：{exc}"}, 500)
            return

        if parsed.path == "/api/live":
            # refresh=1 强制重新查询（默认走 45 秒进程内缓存）
            force = (query.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
            try:
                self._json(live_status(force=force))
            except Exception as exc:  # noqa: BLE001
                logger.exception("实时查询失败")
                self._json({"ok": False, "message": f"实时查询失败：{exc}", "games": []}, 500)
            return

        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path)
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    # ------------------------------ POST ------------------------------ #

    def _origin_ok(self) -> bool:
        """POST 只接受本页面发起的请求。

        页面与服务同源，浏览器一定会带 Origin；带了且不是本机就直接拒。
        没带 Origin 的（curl / 脚本）放行 —— 它们本来就能直接改文件。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        """读取并解析 JSON 请求体；任何异常都已写好响应，返回 None。"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # 强制 JSON 顺带挡掉跨站表单：它只能发 text/plain 或 form-urlencoded
            self._json({"ok": False, "message": "Content-Type 必须是 application/json"}, 415)
            return None

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._json({"ok": False, "message": "请求体为空"}, 400)
            return None
        if length > 64 * 1024:
            self._json({"ok": False, "message": "请求体过大"}, 413)
            return None

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "message": f"JSON 解析失败：{exc}"}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"ok": False, "message": "请求体必须是 JSON 对象"}, 400)
            return None
        return data

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if not self._origin_ok():
            self._json({"ok": False, "message": "拒绝来自其它站点的请求"}, 403)
            return

        path = urlparse(self.path).path

        if path == "/api/push":
            payload = self._read_json_body()
            if payload is not None:
                self._handle_save_push(payload)
            return

        if path == "/api/accounts/enabled":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    games = payload.get("games")
                    if not isinstance(games, list) or not games:
                        one = str(payload.get("game") or "").strip()
                        games = [one] if one else []
                    games = [str(g).strip() for g in games if str(g).strip()]
                    self._json(set_account_enabled(games, _normalize_bool(
                        payload.get("enabled"), True)))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("切换巡检提醒失败")
                    self._json({"ok": False, "message": f"设置失败：{exc}"}, 500)
            return

        if path == "/api/test_push":
            payload = self._read_json_body()
            if payload is not None:
                result = run_test_push(payload)
                self._json(result, 200 if result.get("ok") else 400)
            return

        if path == "/api/device":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_device_text(str(payload.get("text") or "")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存设备档案失败")
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
            return

        if path == "/api/device/reset":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(reset_device_text())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("恢复默认设备档案失败")
                    self._json({"ok": False, "message": f"恢复失败：{exc}"}, 500)
            return

        if path == "/api/schedule":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_schedule(payload.get("times")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存时间点失败")
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
            return

        if path == "/api/catch_up":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_catch_up(payload.get("minutes"), payload.get("enabled")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存补发窗口失败")
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
            return

        if path == "/api/activity_alert":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_activity_alert(
                        payload.get("enabled"), payload.get("days")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存活动提醒开关失败")
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
            return

        if path == "/api/activities/delete":
            from core import activities

            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(activities.ignore_activity(str(payload.get("id") or "")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("删除活动失败")
                    self._json({"ok": False, "message": f"删除失败：{exc}"}, 500)
            return

        if path == "/api/activities/restore":
            from core import activities

            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(activities.restore_ignored())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("恢复活动失败")
                    self._json({"ok": False, "message": f"恢复失败：{exc}"}, 500)
            return

        if path == "/api/activities/refresh":
            from core import activities

            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(activities.refresh(force=True))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("刷新活动缓存失败")
                    self._json({"ok": False, "message": f"刷新失败：{exc}"}, 500)
            return

        if path == "/api/settings":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_thresholds(payload))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存阈值失败")
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
            return

        if path == "/api/autostart":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    from core import autostart

                    self._json(autostart.apply(payload.get("enable")))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("切换开机自启失败")
                    self._json({"ok": False, "message": f"操作失败：{exc}"}, 500)
            return

        if path == "/api/apk/restart":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(restart_tools_service())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("重启 APK 下载服务失败")
                    self._json({"ok": False, "message": f"重启失败：{exc}"}, 500)
            return

        if path in ("/api/daemon/start", "/api/daemon/stop"):
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import daemon as daemon_mod

                result = (
                    daemon_mod.start_daemon()
                    if path.endswith("start")
                    else daemon_mod.stop_daemon()
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("守护进程控制失败")
                result = {"ok": False, "message": f"操作失败：{exc}"}
            status_payload = daemon_status()
            result["daemon"] = status_payload
            self._json(result, 200 if result.get("ok") else 400)
            return

        self._json({"ok": False, "message": "not found"}, 404)

    def _handle_save_push(self, payload: Dict[str, Any]) -> None:
        try:
            raw = load_raw_config()
            result = update_push_config(raw, payload)
        except ValueError as exc:  # 掩码误填等输入问题
            self._json({"ok": False, "message": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取推送配置失败")
            self._json({"ok": False, "message": f"读取配置失败：{exc}"}, 500)
            return

        actions = result["actions"]

        # 一个字都没改就别碰文件：原始 config.json 可能是别的工具生成的，
        # 重新序列化会改变排版，没必要冒这个险。
        if not result["changed"]:
            detail = push_detail()
            detail["actions"] = actions
            detail["changed"] = False
            detail["message"] = "没有任何改动，config.json 未被写入"
            self._json(detail)
            return

        # 写盘前用真正的配置解析器复检：结构不对就一个字都不写
        try:
            from core.config import PushConfig

            PushConfig.from_dict(raw.get("push") or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("推送配置校验未通过，已放弃写入：%s", exc)
            self._json(
                {"ok": False, "message": f"推送配置校验未通过，未写入：{exc}"}, 400
            )
            return

        try:
            save_raw_config(raw)
        except Exception as exc:  # noqa: BLE001
            logger.exception("写入 config.json 失败")
            self._json({"ok": False, "message": f"写入失败：{exc}"}, 500)
            return

        detail = push_detail()  # 重新从磁盘读，确认真的生效
        detail["actions"] = actions
        detail["changed"] = True
        parts = [
            f"{PUSH_CHANNELS[name]['label']}{ACTION_TEXT[action]}"
            for name, action in actions.items()
            if action in ("set", "clear")
        ]
        detail["message"] = "保存成功" + (f"（{'，'.join(parts)}）" if parts else "")
        logger.info("推送配置已保存：%s", detail["message"])
        self._json(detail)



def pick_port(preferred: int, tries: int = 12) -> int:
    """从 preferred 开始找一个真正空闲的端口。

    注意：这里**不能**加 SO_REUSEADDR。Windows 的语义与 Unix 不同 ——
    它允许与"已监听"的端口重复绑定，探测会误判为空闲，结果两个进程
    同时监听同一端口，请求随机落到其中一个，表现为会话莫名失效。
    """
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{preferred}~{preferred + tries - 1} 端口都被占用")


class LocalHTTPServer(ThreadingHTTPServer):
    """只服务本机；Windows 上禁用 allow_reuse_address（原因同 pick_port）。"""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def start_tools_service() -> Optional[Dict[str, Any]]:
    """启动 APK 局域网下载服务（进程内只起一次，重复调用返回同一实例）。

    它监听 0.0.0.0 的独立端口，只读地暴露 tools/ 一层文件；
    主控制台始终只听 127.0.0.1，避免 /api/* 被同一 WiFi 的人调用。

    失败时**不抛异常**，而是把原因写进 :data:`TOOLS_ERROR`——界面要把它
    显示出来，否则用户只看到一句"下载服务未启动"，根本没法自己排查。
    """
    global TOOLS_SERVER, TOOLS_ERROR

    if TOOLS_SERVER is not None:
        return TOOLS_SERVER
    try:
        from core import tools_server

        TOOLS_SERVER = tools_server.start()
        TOOLS_ERROR = "" if TOOLS_SERVER else (tools_server.LAST_ERROR or "下载服务未启动")
    except Exception as exc:  # noqa: BLE001
        TOOLS_ERROR = f"启动异常：{type(exc).__name__}: {exc}"
        logger.warning("APK 下载服务启动失败：%s", TOOLS_ERROR)
        TOOLS_SERVER = None
    if TOOLS_SERVER is None and TOOLS_ERROR:
        logger.warning("APK 下载服务不可用：%s", TOOLS_ERROR)
    return TOOLS_SERVER


def restart_tools_service() -> Dict[str, Any]:
    """关掉现有下载服务、换端口重新起一个，并把新的二维码信息一并返回。

    用于界面上的「换个端口重试」：比如 8790 被别的程序占了、或者用户刚把
    APK 拷进 tools/，点一下就能刷新，不用重启控制台。
    """
    global TOOLS_SERVER, TOOLS_ERROR

    from core import tools_server as ts

    ts.stop(TOOLS_SERVER)
    TOOLS_SERVER = None
    TOOLS_ERROR = ""
    start_tools_service()
    return apk_info(retry=False)


def serve(
    port: int = 8765,
    *,
    lan_tools: bool = True,
    tries: int = 12,
) -> Dict[str, Any]:
    """启动控制台 HTTP 服务并立刻返回（服务跑在后台线程，不阻塞调用方）。

    桌面窗口（app.py）和命令行入口共用这一份实现，避免两处端口 / 初始化
    逻辑漂移。返回 ``{"server", "port", "url", "thread", "tools"}``，
    关闭时调用 :func:`stop_server`。
    """
    ensure_config()
    if lan_tools:
        start_tools_service()

    actual = pick_port(port, tries)
    server = LocalHTTPServer(("127.0.0.1", actual), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="console-http", daemon=True
    )
    thread.start()
    logger.info("控制台已监听 127.0.0.1:%d", actual)
    return {
        "server": server,
        "port": actual,
        "url": f"http://127.0.0.1:{actual}/",
        "thread": thread,
        "tools": TOOLS_SERVER,
    }


def stop_server(info: Optional[Dict[str, Any]]) -> None:
    """关闭 :func:`serve` 起出来的服务（幂等，可重复调用）。"""
    server = (info or {}).get("server")
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        logger.debug("关闭 HTTP 服务时出错（已忽略）", exc_info=True)
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        logger.debug("释放监听端口时出错（已忽略）", exc_info=True)


def console_alive(port: int = 8765, timeout: float = 1.5) -> bool:
    """探测本机某端口上是否已经跑着本控制台（用于复用已开着的实例）。"""
    import urllib.request

    url = f"http://127.0.0.1:{port}/api/env"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return isinstance(data, dict) and "channels" in data
    except Exception:  # noqa: BLE001
        return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ResinLoop · 本地控制台")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--no-lan-tools",
        action="store_true",
        help="不启动 APK 局域网下载服务（手机就没法扫码下载了）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        info = serve(args.port, lan_tools=not args.no_lan_tools)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 控制台启动失败：{exc}")
        return 1

    print("=" * 58)
    print("  ResinLoop · 控制台已启动")
    print(f"  控制台  ：{info['url']}")
    if info["tools"]:
        print(f"  APK 下载：{info['tools']['url']}（手机连同一 WiFi 可访问）")
    else:
        print("  [提示] tools/ 为空或端口被占用，手机扫码下载不可用")
    print("  关闭本窗口只停控制台，定时提醒由守护进程负责")
    print("=" * 58)

    if not args.no_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open(info["url"])),
            daemon=True,
        ).start()

    waiter = threading.Event()
    try:
        while info["thread"].is_alive():
            waiter.wait(0.5)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop_server(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
