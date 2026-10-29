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
import shutil
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
    """把模板里的 __ICON_STAMP__ 换成当前缓存戳。每次请求都算，不缓存。

    页面本体走 get_page()：ui/ 三个文件任一改动都会在下一次请求时自动生效。
    """
    return get_page().replace("__ICON_STAMP__", _icon_stamp())

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
    # 演示模式：出厂配置 provider=mock（还没扫过码）→ 前端显示欢迎横幅
    provider = str((raw.get("query") or {}).get("provider") or "").strip().lower()
    return {
        "channels": channels,
        "ready": ready,
        "configured": bool(ready),
        "always_notify": bool(push.get("always_notify", False)),
        "demo_mode": provider == "mock",
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


#: 滚动备份份数：config.json.bak-1（最新）~ .bak-3（最老），写入前轮转
CONFIG_BACKUP_KEEP = 3
_CONFIG_BAK_LOCK = threading.Lock()


def _backup_config_before_write() -> None:
    """写入前把当前 config.json 滚动备份为 .bak-1/.bak-2/.bak-3。

    原子写防的是「写坏一半」，防不了「写错内容」——上层把配置改坏再保存时，
    靠这三份快照回退。备份失败绝不阻断保存本身，只记 WARNING。
    用独立锁而非 _CONFIG_WRITE_LOCK：调用方有的持锁有的不持，
    后者可重入加锁会死锁；备份只读 config.json、不碰它本身，独立串行化即可。
    """
    try:
        if not CONFIG_PATH.exists():
            return
        with _CONFIG_BAK_LOCK:
            for i in range(CONFIG_BACKUP_KEEP - 1, 0, -1):
                src = CONFIG_PATH.parent / f"{CONFIG_PATH.name}.bak-{i}"
                dst = CONFIG_PATH.parent / f"{CONFIG_PATH.name}.bak-{i + 1}"
                if src.exists():
                    os.replace(src, dst)   # 旧的往后挪，最老的一份被顶掉
            shutil.copy2(CONFIG_PATH,
                         CONFIG_PATH.parent / f"{CONFIG_PATH.name}.bak-1")
    except OSError as exc:
        logger.warning("config.json 滚动备份失败（不影响本次保存）：%s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("config.json 滚动备份出现未预期错误（%s）", exc)


def save_raw_config(raw: Dict[str, Any]) -> None:
    """写回 config.json：先序列化到临时文件并复检，再原子替换。

    直接 write_text 覆盖有个隐患——写到一半断电/被杀，config.json 就成半截
    JSON，之后所有命令都读不了配置。这里改成「临时文件 + os.replace」：
    同一分区内的 replace 是原子操作，失败时原文件完好无损。

    临时文件名用 mkstemp 保证唯一：界面上两个开关几乎同时保存时，
    固定文件名会让两个线程互相删掉对方的临时文件（先写的那次拿不到
    自己的 tmp，finally 清理直接抛错，于是配置写进去了、接口却返回 500）。
    """
    _backup_config_before_write()   # 滚动备份（防"写错内容"，原子写只防"写坏一半"）
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
    try:
        proxy = str((load_raw_config().get("query") or {}).get("proxy") or "")
    except Exception:  # noqa: BLE001 - 配置读不到不影响设备页其它内容
        proxy = ""
    return {
        "ok": True,
        "custom": mihoyo_api.PROFILE_OVERRIDE_PATH.exists(),
        "label": mihoyo_api.identity_label(),
        "proxy": proxy,
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
    result["message"] = "已切回随机设备，请重新扫码"
    return result


def save_query_proxy(raw_value: Any) -> Dict[str, Any]:
    """校验并写入 config.query.proxy（空串 = 直连）。仅本地读写，不做连通性测试。

    消费链：下一轮检查 run_check → _apply_mihoyo_query_config → mihoyo_api.set_proxy，
    getFp 与 dailyNote 均经此出口。
    """
    value = str(raw_value or "").strip()
    if value and not re.match(r"^(https?|socks5h?)://[^\s]+$", value, re.IGNORECASE):
        raise ValueError("代理格式不对：应以 http://、https:// 或 socks5:// 开头，且不含空格")
    raw = load_raw_config()
    query = raw.get("query")
    if not isinstance(query, dict):
        query = {}
        raw["query"] = query
    query["proxy"] = value

    from core.config import parse_config

    parse_config(raw, source=CONFIG_PATH)
    save_raw_config(raw)
    message = "已保存：米游社查询将经代理出口" if value else "已保存：查询直连"
    logger.info("查询代理已更新：%s", value or "(直连)")
    return {"ok": True, "proxy": value, "message": message}


def shuffle_device() -> Dict[str, Any]:
    """随机换一台设备：换机型 + 换 OAID，旧指纹缓存一并作废。"""
    from core import mihoyo_api

    mihoyo_api.shuffle_device_profile()   # 内部已清 device_fp 缓存
    mihoyo_api.reload_device_profile()    # 让 UA 等缓存立刻跟上

    result = device_status()
    result["message"] = "已换一台设备，下次查询会用新指纹"
    logger.info(
        "设备已切换为 %s（%s）",
        result["profile"]["model"],
        result.get("label") or "未知",
    )
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
#: 磁盘缓存：服务重启后也能秒回上一次的结果，页面冷启动绝不该现场打接口
_LIVE_DISK_CACHE = BASE_DIR / "data" / "live_cache.json"


def _live_disk_read() -> Optional[Dict[str, Any]]:
    """读上一次成功查询落盘的结果；没有或坏了返回 None。"""
    try:
        data = json.loads(_LIVE_DISK_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("games"):
        return None
    return data


def _live_disk_write(payload: Dict[str, Any]) -> None:
    """成功查询后原子落盘；写失败只影响下次秒开，不影响本次返回。"""
    try:
        _LIVE_DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LIVE_DISK_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LIVE_DISK_CACHE)
    except OSError:
        logger.exception("实时查询磁盘缓存写入失败")


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
    """返回每款游戏的体力 / 理智看板。

    三级缓存：进程内 45 秒 → 磁盘 data/live_cache.json → 现场查询。
    默认（页面加载）只回缓存，绝不现场打接口 —— 秒开是铁律；
    真正的实时查询只发生在「刷新」按钮（force=1），日常数据由定时守护进程落盘。
    """
    cached = _LIVE_CACHE.get("payload")
    if (not force) and cached is not None and (time.time() - float(_LIVE_CACHE.get("at") or 0)) < _LIVE_TTL_SECONDS:
        payload = dict(cached)
        payload["cached"] = True
        return payload

    # 演示模式（出厂 provider=mock）：拒绝展示模拟假数据，也绝不让假数据进缓存 ——
    # 首次体验应该是引导扫码，而不是"博士 全部完成"这种会让用户以为软件出错的假面板。
    try:
        from core.config import load_config as _lc

        if _lc(CONFIG_PATH).query.provider == "mock":
            return {
                "ok": True,
                "cached": False,
                "demo": True,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "games": [],
            }
    except Exception:  # noqa: BLE001 - 配置读不出就按正常路径走
        pass

    if not force:
        # 进程缓存不在有效期：直接回上一次落盘的结果（标 stale，前端提示可手动刷新）。
        disk = _live_disk_read()
        if disk is not None:
            payload = dict(disk)
            payload["cached"] = True
            payload["stale"] = True
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
                # 距满秒数（API 恢复字段经 normalize_recover_seconds 统一化；无则 None）
                "recover": task.recover_seconds,
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
    _live_disk_write(payload)
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
    from core.mihoyo_api import bind_device, identity_device_id
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
        device_id = identity_device_id()  # 全局唯一安装级设备：扫码与查询共用
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
            session.update("creating",
                           f"第 {index}/{total} 步 · {label}：正在申请二维码（将更新 {total} 款游戏）…")
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

#: 前端三件套（ui/index.html + ui/style.css + ui/app.js）：拼装成单页 HTML。
#: 占位符 __STYLE__ / __APP_JS__ 在 _assemble_page() 里填充；
#: __ICON_STAMP__（图标缓存戳）在每次请求时由 _render_page() 替换。
#: 不再是导入时一次性拼装 —— get_page() 按文件指纹缓存，ui/ 改了自动重载。
#: 拆成独立文件的原因：5800 行 Python 里的 HTML 字符串已不可维护（第三阶段架构项）。
_UI_DIR = BASE_DIR / "ui"
_UI_FILES = ("index.html", "style.css", "app.js")

#: 页面缓存。key = 三个文件的 (mtime_ns, size) 指纹，值 = 拼好的单页 HTML。
#: ui/ 里任一文件在磁盘上发生变化，下一次请求就会自动重建，无需重启进程。
_PAGE_LOCK = threading.Lock()
_PAGE_CACHE: Dict[str, Any] = {"stamp": None, "html": ""}


def _assemble_page() -> str:
    """读取 ui/ 三件套拼成单页 HTML（占位符 __STYLE__ / __APP_JS__ 在此填充）。"""
    html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace(
        "__STYLE__", (_UI_DIR / "style.css").read_text(encoding="utf-8")
    ).replace(
        "__APP_JS__", (_UI_DIR / "app.js").read_text(encoding="utf-8")
    )


def get_page() -> str:
    """取当前页面；ui/ 文件有更新则自动重建（前端热重载）。

    首次读取失败会照旧抛 RuntimeError（ui/ 不完整）；此后遭遇读取异常时
    降级返回上一份已加载的页面，避免临时性 IO 抖动让整个控制台 500。
    """
    stamp = None
    try:
        stamp = tuple(
            (f, int((_UI_DIR / f).stat().st_mtime_ns), (_UI_DIR / f).stat().st_size)
            for f in _UI_FILES
        )
    except OSError:
        pass

    with _PAGE_LOCK:
        cached = _PAGE_CACHE["html"]
        if stamp is None:
            if not cached:
                raise RuntimeError("前端三件套读取失败（ui/ 目录不完整？）")
            return cached
        if _PAGE_CACHE["stamp"] != stamp or not cached:
            try:
                _PAGE_CACHE["html"] = _assemble_page()
                _PAGE_CACHE["stamp"] = stamp
            except OSError as exc:
                if not cached:
                    raise RuntimeError(
                        f"前端三件套读取失败（ui/ 目录不完整？）：{exc}"
                    ) from exc
                logger.warning("重载前端资源失败，继续使用上一版：%s", exc)
        return _PAGE_CACHE["html"]


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

        if parsed.path == "/api/push_history":
            # 最近推送（GET）：读 data/push_history.json（notifier 每次真实发送都落一条），
            # 不再解析日志 —— desktop.log / dailycheck.log 分家曾让前端永远筛不到
            try:
                from core.notifier import load_push_history

                self._json({"ok": True, "items": load_push_history(8)})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "items": [], "message": str(exc)})
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

        if path == "/api/query/proxy":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(save_query_proxy(payload.get("proxy")))
                except ValueError as exc:
                    self._json({"ok": False, "message": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("保存查询代理失败")
                    self._json({"ok": False, "message": "保存失败，请查看运行日志"}, 500)
            return

        if path == "/api/device/shuffle":
            payload = self._read_json_body()
            if payload is not None:
                try:
                    self._json(shuffle_device())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("换设备失败")
                    self._json({"ok": False, "message": f"换设备失败：{exc}"}, 500)
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

        if path == "/api/daemon/run-now":
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import daemon as daemon_mod

                if not daemon_status().get("running"):
                    start = daemon_mod.start_daemon()
                    if not start.get("ok"):
                        self._json({"ok": False,
                                    "message": start.get("message") or "守护进程未能启动，巡检未执行"},
                                   400)
                        return
                daemon_mod.RUN_NOW_FLAG.write_text(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
                self._json({"ok": True, "message": "已开始巡检"})
            except Exception as exc:  # noqa: BLE001
                logger.exception("立即巡检请求失败")
                self._json({"ok": False, "message": "巡检请求执行失败，请查看运行日志"}, 500)
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

    if sys.stdout is None or sys.stderr is None:
        # pythonw.exe 下 std 句柄是 None：print / logging 都会炸。
        # 终端 banner 改投 devnull，进程照常静默驻留（日志走 logs/ 文件）。
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass

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
