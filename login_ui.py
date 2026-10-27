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
        "next_run": target.strftime("%Y-%m-%d %H:%M") if target else "",
        "daemon": daemon_status(),
        # 页面上要列账号就取这里，避免把游戏清单写死在多份 HTML / JS 里
        "enabled_games": _enabled_game_ids(),
        "accounts_info": _enabled_account_info(),
    }


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
            account.setdefault("credential", {})["token"] = token
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
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ResinLoop · 控制台</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --fg:#1a1c1f; --muted:#6b7280;
    --line:#e5e7eb; --brand:#2563eb; --ok:#059669; --warn:#d97706; --err:#dc2626;
    --violet:#7c3aed;
    --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06);
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0f1115; --card:#181b21; --fg:#e8eaed; --muted:#9aa3af;
           --line:#272b33; --brand:#5b8def; --ok:#34d399; --warn:#fbbf24; --err:#f87171;
           --violet:#a78bfa;
           --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3); }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       padding:48px 16px;
       min-height:100vh;display:flex;flex-direction:column;justify-content:center}
  .wrap{width:100%;max-width:820px;margin:0 auto}
  header{margin-bottom:18px}
  h1{font-size:22px;margin:0 0 6px;letter-spacing:-.01em}
  .sub{margin:0;color:var(--muted);font-size:14px}

  /* ---------- 顶部三个导航按钮 ---------- */
  .tabs{display:flex;gap:6px;margin:0 0 20px;padding:5px;background:var(--card);
        border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
  .tabs button{flex:1;padding:9px 8px;border-radius:8px;border:none;background:transparent;
               color:var(--muted);font-size:14px;font-weight:500;cursor:pointer;
               transition:background .15s,color .15s;white-space:nowrap}
  .tabs button:hover{color:var(--fg);opacity:1}
  .tabs button.on{background:var(--brand);color:#fff}
  .tabpanel{display:none}
  .tabpanel.on{display:block}

  .cards{display:grid;gap:14px;grid-template-columns:1fr 1fr}
  .cards .card{display:flex;flex-direction:column}
  .cards .card > button{margin-top:auto}
  @media (max-width:640px){ .cards{grid-template-columns:1fr} }
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:18px;box-shadow:var(--shadow)}
  .card + .card{margin-top:14px}
  .card-head{display:flex;align-items:center;gap:8px;margin-bottom:14px}
  .card h2{font-size:15px;margin:0}
  .dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--muted)}
  .dot.m{background:#4f9cf9}.dot.s{background:#f0a04b}
  .desc{color:var(--muted);font-size:12.5px;margin:0 0 12px}
  .desc code,.hint code{background:var(--bg);padding:1px 5px;border-radius:4px;
                        font-size:12px;border:1px solid var(--line)}
  .checks{display:flex;flex-direction:column;gap:7px;margin-bottom:14px}
  .checks label{display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;
                padding:2px 0}
  .checks input{width:15px;height:15px;accent-color:var(--brand);cursor:pointer;margin:0}
  .all{color:var(--muted);font-size:13px;border-bottom:1px dashed var(--line);
       padding-bottom:8px;margin-bottom:2px}
  button{width:100%;padding:10px 14px;border-radius:9px;border:1px solid transparent;
         background:var(--brand);color:#fff;font-size:14px;font-weight:500;cursor:pointer;
         transition:opacity .15s}
  button:hover{opacity:.9}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:transparent;border-color:var(--line);color:var(--muted);
               width:auto;padding:8px 14px;margin-top:14px}
  button.alt{background:transparent;border-color:var(--line);color:var(--fg)}
  button.alt:hover{border-color:var(--brand);color:var(--brand);opacity:1}
  .stage{margin-top:14px;background:var(--card);border:1px solid var(--line);
         border-radius:14px;padding:22px;box-shadow:var(--shadow);text-align:center}
  .qrbox{display:inline-flex;padding:12px;background:#fff;border-radius:12px;
         border:1px solid var(--line);min-height:224px;min-width:224px;
         align-items:center;justify-content:center}
  .qrbox img{width:224px;height:224px;display:block;image-rendering:pixelated}
  .qrbox .ph{color:#9aa3af;font-size:13px;padding:0 20px}
  .status{margin-top:16px;display:flex;align-items:center;justify-content:center;gap:8px;
          flex-wrap:wrap}
  .badge{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);
         color:var(--muted);white-space:nowrap}
  .badge.ok{color:var(--ok);border-color:var(--ok)}
  .badge.warn{color:var(--warn);border-color:var(--warn)}
  .badge.err{color:var(--err);border-color:var(--err)}
  #msg{margin:0;font-size:14px}
  .kv{margin-top:14px;font-size:13px;color:var(--muted);text-align:left;
      border-top:1px solid var(--line);padding-top:12px}
  .kv code{background:var(--bg);padding:1px 6px;border-radius:5px;font-size:12px}
  .res{display:flex;flex-direction:column;gap:4px;margin-top:8px}
  .res div{display:flex;align-items:center;gap:6px}
  .res .i-ok{color:var(--ok)}.res .i-no{color:var(--err)}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);
        border-top-color:var(--brand);border-radius:50%;animation:s .8s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
  .notice{margin:0 0 18px;padding:12px 14px;border-radius:10px;font-size:13px;
          line-height:1.7;border:1px solid var(--warn);background:rgba(217,119,6,.08);
          color:var(--fg)}
  .notice b{color:var(--warn)}
  .notice.info{border-color:var(--line);background:var(--card)}
  .notice.info b{color:var(--fg)}
  .notice a{color:var(--brand);text-decoration:none;font-weight:500;
            border-bottom:1px solid currentColor;cursor:pointer}

  /* ---------- 通知绑定 ---------- */
  .stack{display:grid;gap:14px}
  .chan-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
             margin-bottom:6px}
  .chan-head h2{font-size:15px;margin:0;display:flex;align-items:center;gap:8px}
  .switch{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);
          cursor:pointer;white-space:nowrap;user-select:none}
  .switch input{width:15px;height:15px;accent-color:var(--brand);cursor:pointer;margin:0}
  /* ---------- 手风琴（定时与状态） ---------- */
  .acc-item{background:var(--card);border:1px solid var(--line);border-radius:14px;
    box-shadow:var(--shadow);overflow:hidden;margin-top:10px}
  .card + .acc-item{margin-top:14px}
  .acc-sep + .acc-item{margin-top:0}
  .acc-item.soft{background:var(--bg)}
  .acc-head{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:14px 16px;cursor:pointer;user-select:none;list-style:none;
    -webkit-tap-highlight-color:transparent;transition:background .15s}
  .acc-head::-webkit-details-marker{display:none}
  .acc-head:hover{background:rgba(37,99,235,.035)}
  .acc-head::before{content:"";width:0;height:0;flex:none;
    border-left:5px solid var(--muted);border-top:4px solid transparent;
    border-bottom:4px solid transparent;transform-origin:2px 50%;
    transition:transform .18s ease}
  .acc-item[open] > .acc-head::before{transform:rotate(90deg)}
  .acc-title{display:flex;align-items:center;gap:8px;flex:1;min-width:0;
    font-size:14.5px;font-weight:600;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis}
  .acc-note{font-weight:400;font-size:12px;color:var(--muted);
    overflow:hidden;text-overflow:ellipsis}
  .acc-actions{display:flex;gap:8px;flex:none}
  .mini{width:auto;padding:5px 13px;font-size:12.5px;border-radius:7px;line-height:1.5}
  .mini.alt{background:transparent;border-color:var(--line);color:var(--fg)}
  .mini.alt:hover{border-color:var(--brand);color:var(--brand);opacity:1}
  /* 运行中：启动按钮变灰不可点，一眼看出是"已经在跑"而不是坏了 */
  .mini:disabled{background:var(--line);color:var(--muted);border-color:transparent;
    opacity:1;cursor:not-allowed}
  .acc-body{padding:2px 16px 16px;border-top:1px solid var(--line)}
  .acc-body .daemon-core{margin-top:14px}
  .acc-body .desc{margin-top:12px;margin-bottom:0}
  /* 总开关关掉时，展开内容淡淡地"退后一步"（仍可编辑，只是提示当前不生效） */
  .acc-item.is-off .acc-body{opacity:.6}
  .acc-item.is-off .acc-body:focus-within{opacity:1}
  /* 粗一点的浅灰分割线：把「当前运行」和「开机行为」在视觉上切成两组 */
  .acc-sep{height:4px;border-radius:999px;background:#d7dbe1;margin:18px 8px}
  @media (prefers-color-scheme: dark){ .acc-sep{background:#343a44} }
  @media (max-width:520px){
    .acc-head{padding:12px;gap:8px}
    .acc-title{font-size:13.5px}
    .acc-note{display:none}
    .mini{padding:4px 10px;font-size:12px}
  }

  /* 现代拨动开关（纯 CSS，无图片） */
  .toggle{position:relative;display:inline-block;width:42px;height:24px;flex:none}
  .toggle input{position:absolute;opacity:0;width:0;height:0;margin:0}
  .toggle i{position:absolute;inset:0;border-radius:999px;background:var(--line);
    transition:background .18s ease;pointer-events:none}
  .toggle i::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;
    border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(16,24,40,.25);
    transition:transform .18s ease}
  .toggle input:checked + i{background:var(--brand)}
  .toggle input:checked + i::after{transform:translateX(18px)}
  .toggle input:focus-visible + i{box-shadow:0 0 0 3px rgba(37,99,235,.25)}
  .toggle input:disabled + i{opacity:.45}

  /* 守护进程核心信息：两行并排 */
  .daemon-core{display:grid;grid-template-columns:1fr 1fr;gap:10px 18px;
    margin-top:2px;font-size:13.5px}
  @media (max-width:520px){ .daemon-core{grid-template-columns:1fr} }
  .daemon-core > div{display:flex;justify-content:space-between;gap:10px;
    padding:8px 0;border-bottom:1px dashed var(--line)}
  .daemon-core span{color:var(--muted);white-space:nowrap}
  .daemon-core b{font-weight:600;text-align:right;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}

  /* 已运行时"启动"按钮置灰，但仍保留占位不让布局跳动 */
  #btn-daemon-start:disabled{opacity:.45;cursor:not-allowed}
  .field{margin-top:4px}
  .field input,.field textarea{width:100%;padding:10px 12px;border-radius:9px;
               border:1px solid var(--line);background:var(--bg);color:var(--fg);
               font-size:13.5px}
  .field input{font-family:ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace}
  .field textarea{font:12.5px/1.55 ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;
                  resize:vertical;min-height:110px}
  .field input:focus,.field textarea:focus{outline:none;border-color:var(--brand);
                     box-shadow:0 0 0 3px rgba(37,99,235,.14)}
  .field input::placeholder,.field textarea::placeholder{color:var(--muted);opacity:.65}
  .hint{margin:7px 0 0;font-size:12px;color:var(--muted);line-height:1.55}
  .hint b{color:var(--fg)}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
  @media (max-width:520px){ .row{grid-template-columns:1fr} }
  /* ---------- 每日报表 ---------- */
  .hstats{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(128px,1fr))}
  /* 极简卡：上游戏名、下大数字，垂直居中；本月明细走 title 悬浮提示 */
  .hstat{background:var(--bg);border:1px solid var(--line);border-radius:10px;
         padding:12px 8px;display:flex;flex-direction:column;align-items:center;
         justify-content:center;gap:3px;text-align:center;cursor:default;
         transition:border-color .15s,background .15s}
  .hstat:hover{border-color:var(--brand)}
  .hstat .name{font-size:12px;color:var(--muted);max-width:100%;
               overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hstat .big{font-size:24px;font-weight:600;line-height:1.1;letter-spacing:-.02em}
  .hstat .big small{font-size:11.5px;font-weight:400;color:var(--muted);margin-left:3px}
  .hstat.is-off{opacity:.55}
  .htable{width:100%;border-collapse:collapse;font-size:13px;margin-top:2px}
  .htable th,.htable td{padding:7px 4px;border-bottom:1px solid var(--line);
                        text-align:center}
  .htable th{font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}
  .htable td{font-size:15px}
  .htable td.date{text-align:left;white-space:nowrap;font-size:12.5px}
  .htable td.date .wd{color:var(--muted);font-size:11.5px;margin-left:4px}
  .htable tr.today td{background:rgba(37,99,235,.06)}
  .htable tr:last-child td{border-bottom:none}
  .emptybox{padding:18px 0;text-align:center;color:var(--muted);font-size:13px;
            line-height:1.7}
  #test-result{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
  #toast{position:fixed;left:50%;top:20px;transform:translate(-50%,-14px);
         background:var(--fg);color:var(--bg);padding:10px 18px;border-radius:10px;
         font-size:14px;font-weight:500;opacity:0;pointer-events:none;z-index:99;
         transition:opacity .2s,transform .2s;box-shadow:0 10px 30px rgba(0,0,0,.22);
         max-width:88vw;text-align:center}
  #toast.show{opacity:1;transform:translate(-50%,0)}
  #toast.err{background:var(--err);color:#fff}

  /* ---------- 设备伪装 ---------- */
  .dev-top{display:grid;grid-template-columns:1fr 150px;gap:16px;align-items:start}
  @media (max-width:600px){ .dev-top{grid-template-columns:1fr} }
  .steps{display:flex;flex-direction:column;gap:8px;margin:0 0 14px}
  .step{display:flex;gap:9px;align-items:flex-start;font-size:13px;color:var(--muted)}
  .step b{flex:none;width:19px;height:19px;border-radius:50%;background:var(--brand);
          color:#fff;font-size:11.5px;display:flex;align-items:center;justify-content:center;
          margin-top:1px}
  .apk{text-align:center}
  .apk-qr{display:inline-block;padding:8px;background:#fff;border:1px solid var(--line);
          border-radius:10px}
  .apk-qr img{width:126px;height:126px;display:block}
  .apk-qr button{width:auto;padding:6px 12px;font-size:12px;border-radius:7px}
  .apk-meta{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.4;
            word-break:break-all}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
        word-break:break-all}

  /* ---------- 定时与状态 ---------- */
  .kv2{display:grid;grid-template-columns:1fr 1fr;gap:10px 18px;margin-top:12px;
       font-size:13px}
  @media (max-width:520px){ .kv2{grid-template-columns:1fr} }
  .kv2 > div{display:flex;justify-content:space-between;gap:10px;
             border-bottom:1px dashed var(--line);padding-bottom:6px}
  .kv2 span{color:var(--muted)}
  .kv2 b{font-weight:500;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
         font-size:12.5px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
  .chip{display:inline-flex;align-items:center;gap:6px;background:var(--bg);
        border:1px solid var(--line);border-radius:999px;padding:5px 6px 5px 12px;
        font-size:13.5px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
  .chip button{width:20px;height:20px;padding:0;border-radius:50%;background:transparent;
               color:var(--muted);border:none;font-size:15px;line-height:1;cursor:pointer}
  .chip button:hover{background:var(--err);color:#fff;opacity:1}
  .chip.empty{color:var(--muted);border-style:dashed;padding:5px 12px;font-family:inherit}
  .row3{display:grid;grid-template-columns:1fr auto;gap:10px;margin-top:14px}
  .row3 button{width:auto;padding:10px 18px}
  /* 体力溢出阈值三栏：窄屏变单列 */
  .resin-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:4px 0 14px}
  @media (max-width:640px){ .resin-row{grid-template-columns:1fr} }
  .resin-row label{display:block;font-size:12.5px;color:var(--muted)}
  .resin-row input[type=number]{
    width:100%;margin-top:4px;padding:8px 10px;border-radius:9px;
    border:1px solid var(--line);background:var(--bg);color:var(--fg);
    font-size:14px;font-family:ui-monospace,Consolas,monospace}
  .resin-row input[type=number]:focus{outline:none;border-color:var(--brand);
    box-shadow:0 0 0 3px rgba(37,99,235,.14)}
  input[type=time]{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line);
                   background:var(--bg);color:var(--fg);font-size:14px;
                   font-family:ui-monospace,Consolas,monospace}
  input[type=time]:focus{outline:none;border-color:var(--brand);
                         box-shadow:0 0 0 3px rgba(37,99,235,.14)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>ResinLoop · 控制台</h1>
    <p class="sub">每日报表看结果，账号绑定解决「查得到」，通知绑定解决「收得到」，定时与状态负责「一直跑」</p>
  </header>

  <nav class="tabs">
    <button type="button" data-tab="history" class="on">每日报表</button>
    <button type="button" data-tab="account">账号绑定</button>
    <button type="button" data-tab="notify">通知绑定</button>
    <button type="button" data-tab="schedule">定时与状态</button>
  </nav>

  <!-- ======================= Tab 1：账号绑定 ======================= -->
  <section class="tabpanel" id="tab-account">
    <div class="notice" id="notice" style="display:none"></div>

    <div class="cards">
      <section class="card">
        <div class="card-head"><span class="dot m"></span><h2>米游社</h2></div>
        <div class="checks">
          <label class="all"><input type="checkbox" id="m-all" checked
                 onchange="toggleAll('m', this.checked)"> 全选</label>
          <label><input type="checkbox" name="m" value="genshin" checked> 原神</label>
          <label><input type="checkbox" name="m" value="starrail" checked> 崩坏：星穹铁道</label>
          <label><input type="checkbox" name="m" value="zzz" checked> 绝区零</label>
        </div>
        <button id="btn-m" onclick="startMihoyo()">登录所选</button>
      </section>

      <section class="card">
        <div class="card-head"><span class="dot s"></span><h2>森空岛</h2></div>
        <div class="checks">
          <label class="all"><input type="checkbox" id="s-all" checked
                 onchange="toggleAll('s', this.checked)"> 全选</label>
          <label><input type="checkbox" name="s" value="arknights" checked> 明日方舟</label>
          <label><input type="checkbox" name="s" value="endfield" checked> 明日方舟：终末地</label>
        </div>
        <button id="btn-s" onclick="startSkland()">登录所选</button>
      </section>
    </div>

    <section class="stage" id="stage" hidden>
      <div class="qrbox" id="qrbox"><span class="ph">二维码准备中…</span></div>
      <div class="status">
        <span class="badge" id="badge">准备中</span>
        <p id="msg"></p>
      </div>
      <div class="kv" id="detail" hidden></div>
      <button class="ghost" onclick="reset()">重新开始</button>
    </section>

    <section class="card" id="dev-card">
      <div class="chan-head">
        <h2><span class="dot" style="background:var(--violet)"></span>设备伪装（可选，但强烈推荐）</h2>
        <span class="badge" id="dev-badge">读取中</span>
      </div>
      <p class="desc">
        米游社按「设备指纹」判断风险。用你自己手机的真实档案，通过率最高；
        不填则用内置的通用机型。
      </p>

      <div class="dev-top">
        <div>
          <div class="steps">
            <div class="step"><b>1</b><span>手机扫码下载并安装「设备信息」App</span></div>
            <div class="step"><b>2</b><span>打开 App，点「复制设备信息」</span></div>
            <div class="step"><b>3</b><span>把复制到的内容粘到下面，点保存</span></div>
          </div>
          <div class="field">
            <textarea id="dev-text" spellcheck="false"
              placeholder='把「设备信息」App 复制出来的内容粘贴到这里，形如：
{"deviceModel":"24122RKC7C","androidVersion":"16","deviceFingerprint":"...","oaid":"..."}'></textarea>
          </div>
        </div>
        <div class="apk" id="apk-box">
          <div class="apk-qr" id="apk-qr"><span class="ph" style="font-size:12px;color:#9aa3af">二维码生成中…</span></div>
          <div class="apk-meta" id="apk-meta"></div>
        </div>
      </div>

      <p class="hint" id="dev-current">正在读取当前设备档案…</p>
      <div class="row">
        <button id="btn-dev-save">保存并清空指纹缓存</button>
        <button id="btn-dev-reset" class="alt">恢复默认</button>
      </div>
      <p class="hint">
        保存后会删掉 <code>logs/device_fp.json</code>，下次查询自动按新档案重签指纹。
        <b>保存后必须重新扫码</b>，否则登录设备与查询设备不一致。
      </p>
    </section>
  </section>

  <!-- ======================= Tab 2：通知绑定 ======================= -->
  <section class="tabpanel" id="tab-notify">
    <div class="notice info" style="display:block">
      Key 由后端打码后再传到页面，网页里看不到明文。
      <b>不修改输入框 = 保持原值</b>；<b>清空后保存 = 删除该 Key 并退出推送列表</b>。
    </div>

    <div class="stack">
      <section class="card">
        <div class="chan-head">
          <h2><span class="dot" style="background:#07c160"></span>Server酱（微信）</h2>
          <label class="switch"><input type="checkbox" id="serverchan-enabled"> 启用</label>
        </div>
        <p class="desc">微信扫码登录 <code>sct.ftqq.com</code> 领取 SendKey，以 <code>SCT</code> 开头，推送直达微信。</p>
        <div class="field">
          <input type="text" id="serverchan-key" autocomplete="off" spellcheck="false"
                 autocapitalize="off" placeholder="SCT 开头的 SendKey">
        </div>
        <p class="hint" id="serverchan-hint">正在读取…</p>
      </section>

      <section class="card">
        <div class="chan-head">
          <h2><span class="dot" style="background:#12b7f5"></span>Qmsg 酱（QQ）</h2>
          <label class="switch"><input type="checkbox" id="qmsg-enabled"> 启用</label>
        </div>
        <p class="desc">在 <code>qmsg.zendee.cn</code> 用机器人 QQ 登录后复制 key，推送直达 QQ。</p>
        <div class="field">
          <input type="text" id="qmsg-key" autocomplete="off" spellcheck="false"
                 autocapitalize="off" placeholder="Qmsg 酱的 key">
        </div>
        <p class="hint" id="qmsg-hint">正在读取…</p>
      </section>
    </div>

    <section class="card" style="margin-top:14px">
      <div class="chan-head">
        <h2><span class="dot" style="background:var(--brand)"></span>平安通知</h2>
        <label class="switch"><input type="checkbox" id="always-notify"> 启用</label>
      </div>
      <p class="desc">
        默认只在「有未完成项」时推送。勾上之后，日常<b>全部做完也发一条</b>
        「今日全部完成」——方便确认它真的在跑，不会误以为坏了。
      </p>
    </section>

    <div class="row">
      <button type="button" id="btn-save">保存配置</button>
      <button type="button" id="btn-test" class="alt">发送测试推送</button>
    </div>
    <p class="hint">
      「发送测试推送」用输入框里的当前值试发一条，<b>不会写盘</b>；想留下配置请先点「保存配置」。
    </p>
    <div id="test-result" hidden></div>
  </section>

  <!-- ======================= Tab 3：定时与状态 ======================= -->
  <section class="tabpanel" id="tab-schedule">

    <!-- 1. 提醒时间点：常驻展开（最常用，不折叠） -->
    <section class="card">
      <div class="chan-head">
        <h2><span class="dot" style="background:var(--ok)"></span>提醒时间点</h2>
        <span class="badge" id="d-count">-</span>
      </div>
      <p class="desc">每天到这些时间点检查一次；同一分钟只会提醒一次，不会重复推送。</p>
      <div class="chips" id="d-times"></div>
      <div class="row3">
        <input type="time" id="d-new-time" value="22:00" step="60">
        <button id="btn-add-time">添加</button>
      </div>
      <p class="hint">改完立即生效，<b>不用重启守护进程</b>。</p>
    </section>

    <!-- 2. 守护进程：默认折叠，标题栏右侧放启动/停止 -->
    <details class="acc-item" id="acc-daemon">
      <summary class="acc-head">
        <span class="acc-title">
          <span class="dot" id="d-dot"></span>守护进程
          <span class="badge" id="d-state">读取中</span>
        </span>
        <span class="acc-actions">
          <button type="button" class="mini" id="btn-daemon-start">启动</button>
          <button type="button" class="mini alt" id="btn-daemon-stop">停止</button>
        </span>
      </summary>
      <div class="acc-body">
        <div class="daemon-core">
          <div><span>下次提醒</span><b id="d-next">-</b></div>
          <div><span>最近一次</span><b id="d-last">-</b></div>
        </div>
        <div class="kv2">
          <div><span>进程 PID</span><b id="d-pid">-</b></div>
          <div><span>启动时间</span><b id="d-since">-</b></div>
          <div><span>参与检查</span><b id="d-games">-</b></div>
          <div><span>心跳</span><b id="d-heart">-</b></div>
        </div>
        <p class="hint">
          守护进程只负责「到点检查 + 推送」。关掉本网页不影响它；
          要彻底停掉就点上面的「停止」，或在右下角托盘图标上右键 → 退出程序。
        </p>
      </div>
    </details>

    <!-- 分割线：下面这些是「开机行为」，与当前是否在跑无关 -->
    <div class="acc-sep" role="separator"></div>

    <!-- 3. 开机自启：默认折叠，右侧 Toggle -->
    <details class="acc-item soft" id="acc-autostart">
      <summary class="acc-head">
        <span class="acc-title">下次开机自动运行
          <span class="acc-note">（独立于当前运行状态）</span>
        </span>
        <label class="toggle" title="开启后静默挂载在托盘，彻底关闭需点停止守护进程">
          <input type="checkbox" id="autostart-toggle">
          <i></i>
        </label>
      </summary>
      <div class="acc-body">
        <p class="hint">
          当前状态：<b class="badge" id="autostart-state">读取中</b>
        </p>
        <p class="hint" id="autostart-hint">读取中…</p>
        <p class="hint">
          写的是当前用户的注册表（<code>HKCU\…\Run</code>），<b>不需要管理员权限</b>；
          它决定的是<b>下次登录 Windows</b> 时会不会自动拉起托盘，
          和左边的「停止」互不影响 —— 停止只停这一次，不会关掉开机自启。
        </p>
      </div>
    </details>

    <!-- 4. 睡过头补发：默认折叠，右侧总开关 -->
    <details class="acc-item" id="acc-catchup">
      <summary class="acc-head">
        <span class="acc-title">睡过头补发
          <span class="acc-note" id="cu-state">-</span>
        </span>
        <label class="toggle" title="合盖休眠错过时间点后，醒来补跑一次">
          <input type="checkbox" id="catch-up-toggle">
          <i></i>
        </label>
      </summary>
      <div class="acc-body">
        <p class="desc">
          合盖休眠、关机、断网都可能让那个整点悄悄过去。开启后，在下面的补发窗口内一旦醒来，
          守护进程会立刻补跑一次（同一天同一个时间点只会补一次）。
        </p>
        <div class="row3">
          <input type="number" id="catch-up-minutes" min="0" max="720" step="30">
          <button id="btn-catch-up">保存</button>
        </div>
        <p class="hint">
          单位分钟，最大 720。<b>关掉上方开关后，补发窗口不再生效</b>，但数值会保留，重新打开即恢复。
        </p>
      </div>
    </details>

    <!-- 5. 体力溢出提醒：默认折叠，右侧总开关 -->
    <details class="acc-item" id="acc-resource">
      <summary class="acc-head">
        <span class="acc-title">体力溢出提醒
          <span class="acc-note" id="resin-state">读取中</span>
        </span>
        <label class="toggle" title="当前体力 ≥ 阈值时额外推送一条">
          <input type="checkbox" id="resin-toggle">
          <i></i>
        </label>
      </summary>
      <div class="acc-body">
        <p class="desc">
          当前体力 / 树脂 / 电量 / 理智 ≥ 阈值时强制推送一条提醒。
          <b>0 = 关闭该游戏</b>，留空则用默认值。改完下次检查自动生效，<b>不用重启守护进程</b>。
        </p>
        <div class="resin-row">
          <label>
            <span>原神 · 原粹树脂</span>
            <input type="number" id="resin-genshin" min="0" max="1000" step="10"
                   placeholder="160" inputmode="numeric">
          </label>
          <label>
            <span>星铁 · 开拓力</span>
            <input type="number" id="resin-starrail" min="0" max="1000" step="10"
                   placeholder="240" inputmode="numeric">
          </label>
          <label>
            <span>绝区零 · 电量</span>
            <input type="number" id="resin-zzz" min="0" max="1000" step="10"
                   placeholder="200" inputmode="numeric">
          </label>
          <label>
            <span>明日方舟 · 理智</span>
            <input type="number" id="resin-arknights" min="0" max="1000" step="10"
                   placeholder="135" inputmode="numeric">
          </label>
          <label>
            <span>终末地 · 理智</span>
            <input type="number" id="resin-endfield" min="0" max="1000" step="10"
                   placeholder="300" inputmode="numeric">
          </label>
        </div>
        <button id="btn-resin-save">保存阈值</button>
        <p class="hint" id="resin-hint">读取中…</p>
      </div>
    </details>
  </section>

  <!-- ======================= Tab 4：每日报表 ======================= -->
  <section class="tabpanel on" id="tab-history">
    <section class="card">
      <div class="chan-head">
        <h2><span class="dot" style="background:var(--ok)"></span>连续完成</h2>
        <span class="badge" id="h-month">-</span>
      </div>
      <p class="desc">
        每次定时检查的结果会记在 <code>data/history.json</code>；
        判定用的就是那一轮查询的数据，<b>不额外发任何请求</b>。
      </p>
      <div class="hstats" id="h-stats"></div>
      <p class="hint">
        当天只要有<b>一次</b>检查到全部完成，这天就算 ✅（白天补完也算）；
        查询失败不算未完成，避免网络抖动被记成漏做。
      </p>
    </section>

    <section class="card">
      <div class="chan-head">
        <h2><span class="dot" style="background:var(--brand)"></span>最近 7 天</h2>
        <button class="alt" id="btn-history-refresh" style="width:auto;padding:6px 12px;
               font-size:12px">刷新</button>
      </div>
      <div id="h-table"></div>
      <p class="hint">
        ✅ 完成 · ❌ 查过但没做完 · <span style="color:#9aa3af">–</span> 那天没运行或没查到。
        切到本页才会拉一次数据，不会后台轮询。
      </p>
    </section>
  </section>
</div>

<div id="toast"></div>

<script>
/* =========================== Tab 切换 =========================== */
const PANEL_NAMES = ['history', 'account', 'notify', 'schedule'];
let schedTimer = null;

function switchTab(name){
  PANEL_NAMES.forEach(function(k){
    const panel = document.getElementById('tab-' + k);
    if (panel) {
      panel.style.display = (k === name) ? 'block' : 'none';
      panel.classList.toggle('on', k === name);
    }
    const btn = document.querySelector('.tabs button[data-tab="' + k + '"]');
    if (btn) btn.classList.toggle('on', k === name);
  });

  if (name === 'notify') loadPush();
  if (name === 'schedule') { loadSchedule(); startSchedPoll(); loadThresholds(); loadAutostart(); } else { stopSchedPoll(); }
  if (name === 'account') { loadDevice(); refreshNotice(); }
  if (name === 'history') loadHistory();
}

function startSchedPoll(){
  stopSchedPoll();
  schedTimer = setInterval(loadSchedule, 3000);
}
function stopSchedPoll(){
  if (schedTimer) { clearInterval(schedTimer); schedTimer = null; }
}

document.querySelectorAll('.tabs button[data-tab]').forEach(function(btn){
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

/* =========================== Tab 1：账号绑定 =========================== */
let sid = null, timer = null;

function toggleAll(group, checked){
  document.querySelectorAll('input[name="' + group + '"]').forEach(function(i){
    i.checked = checked;
  });
}

function selected(group){
  return [...document.querySelectorAll('input[name="' + group + '"]:checked')]
           .map(function(i){ return i.value; });
}

function syncAllBox(group){
  const boxes = [...document.querySelectorAll('input[name="' + group + '"]')];
  const all = document.getElementById(group + '-all');
  const on = boxes.filter(function(b){ return b.checked; }).length;
  all.checked = on === boxes.length;
  all.indeterminate = on > 0 && on < boxes.length;
}

document.querySelectorAll('input[name="m"], input[name="s"]').forEach(function(box){
  box.addEventListener('change', function(){ syncAllBox(box.name); });
});

function badgeFor(state){
  return {
    creating:['准备中',''], waiting:['等待扫码','warn'], scanned:['已扫码','warn'],
    writing:['写入中',''], done:['完成','ok'], error:['失败','err']
  }[state] || [state,''];
}

function setBusy(busy){
  document.getElementById('btn-m').disabled = busy;
  document.getElementById('btn-s').disabled = busy;
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
    box.innerHTML = '<span class="ph"><span class="spin"></span> 请稍候…</span>';
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

function startMihoyo(){
  const list = selected('m');
  if (!list.length) { alert('请至少勾选一款游戏'); return; }
  start('mihoyo', list);
}

function startSkland(){
  const list = selected('s');
  if (!list.length) { alert('请至少勾选一款游戏'); return; }
  start('skland', list);
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
      if (d.state === 'done' || d.state === 'error') { setBusy(false); return; }
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
      '<b>还没配置推送通道</b> —— 扫码登录只负责拿到账号，<b>手机上不会收到任何提醒</b>。<br>' +
      '去 <a data-goto="notify">「通知绑定」</a> 填一个：Server酱 走微信最简单，Qmsg 酱 走 QQ。';
  }).catch(function(){});
}

/* ---------- 设备伪装 ---------- */
function renderDevice(d){
  if (!d.ok) { toast(d.message || '读取设备档案失败', true); return; }
  const p = d.profile || {};
  document.getElementById('dev-badge').textContent = d.custom ? '已使用自定义档案' : '内置默认档案';
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
      (apk.warning ? '<br><span style="color:#b45309">⚠ ' + apk.warning + '</span>' : '');
  } else {
    // 失败时把真实原因显示出来，并给一个「换个端口重试」的按钮
    qrBox.innerHTML =
      '<div style="padding:10px 8px;text-align:center">' +
      '<div style="font-size:12px;color:#9aa3af;line-height:1.5">二维码不可用</div>' +
      '<button class="alt" id="btn-apk-retry">换个端口重试</button>' +
      '</div>';
    meta.innerHTML = '<span style="color:#b45309">' + (apk.message || '未找到 APK') + '</span>';
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
      meta.innerHTML = '<span style="color:#b45309">' + (d.message || '仍然不可用') + '</span>';
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
  if (hint) {
    hint.innerHTML = '默认值：原神 160、星铁 240、绝区零 200、方舟理智 135、终末地理智 300（写 0 关闭）';
  }
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

/* =========================== Tab 2：通知绑定 =========================== */
const CHANNELS = ['serverchan', 'qmsg'];

function hintText(it){
  if (!it.has_key && !it.enabled) return '未配置，也未启用';
  if (!it.has_key) return '已勾选启用，但<b>没有 Key</b> —— 不会推送';
  if (!it.in_channels) return 'Key 已保存，但当前<b>不在推送列表</b>中（勾选启用后保存即加入）';
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
  refreshNotice();
}

function loadPush(){
  fetch('/api/push').then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok) { toast(d.message || '读取配置失败', true); return; }
    renderPush(d);
  }).catch(function(){ toast('无法连接本地服务', true); });
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
    toast(allOk ? '测试消息已发出，看下手机' : '部分通道发送失败，看下方原因', !allOk);
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

/* =========================== Tab 3：定时与状态 =========================== */
function renderDaemon(st){
  const dot = document.getElementById('d-dot');
  const badge = document.getElementById('d-state');
  const running = !!st.running;
  dot.style.background = running ? 'var(--ok)' : 'var(--err)';
  badge.textContent = running ? '运行中' : '已停止';
  badge.className = 'badge ' + (running ? 'ok' : 'err');

  document.getElementById('d-pid').textContent = st.pid || '-';
  document.getElementById('d-since').textContent = st.started_at || '-';
  const heart = document.getElementById('d-heart');
  if (heart) {
    heart.textContent = (st.heartbeat_age != null)
      ? Math.round(st.heartbeat_age) + ' 秒前' : '-';
  }

  // 「最近一次」只留结论 + 月-日 时:分，别把秒和完整年份糊在脸上
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
  document.getElementById('d-count').textContent = times.length + ' 个';
  document.getElementById('d-next').textContent = d.next_run || '-';

  // 参与检查的账号：折叠面板里只给数量，完整清单走 title（来自后端，不写死）
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
    else badge.textContent = '窗口为 0，不补发';
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
      badge.className = 'badge ok';
    } else {
      badge.textContent = '未开启';
      badge.className = 'badge';
    }
  }
  if (hint) {
    if (!supported) {
      hint.textContent = (d && d.message) || '仅支持 Windows';
    } else if (d.stale) {
      hint.innerHTML = '注册表里存的还是旧路径（搬过目录或换过 Python），' +
                       '开机大概率起不来 —— 把开关关掉再打开一次即可刷新。';
    } else if (d.enabled) {
      hint.innerHTML = '已写入注册表：<b>下次登录 Windows</b> 会自动在右下角托盘启动，无需再双击 start.bat。';
    } else {
      hint.innerHTML = '当前未写入注册表；勾选后<b>下次登录 Windows</b> 会自动在右下角托盘启动。';
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
document.getElementById('resin-toggle')
  .addEventListener('change', toggleResourceAlert);

/* 折叠面板标题栏里的开关 / 小按钮：只做自己的事，不连带展开或收起面板。
   stopPropagation 拦住冒泡，summary 就不会收到这次点击（默认的展开动作也就不会发生）。 */
document.querySelectorAll('.acc-head .toggle, .acc-head .mini').forEach(function(el){
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
    // 「停止」只停本次，不能说成"关掉了自启"——这两件事完全独立
    const okMsg = action === 'stop'
      ? '已停止本次运行，开机自启设置保持不变，下次开机仍会启动'
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

/* =========================== Tab 4：每日报表 =========================== */
function markOf(cell){
  if (!cell || !cell.known) return '<span style="color:#9aa3af">–</span>';
  return cell.finished ? '✅' : '❌';
}

function renderHistoryEmpty(message, detail){
  // 空状态：统计卡片清空，表格位置放一句人话（绝不弹红色错误条）
  const statsBox = document.getElementById('h-stats');
  if (statsBox) statsBox.innerHTML = '';
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = '-';
  const tableBox = document.getElementById('h-table');
  if (!tableBox) return;
  tableBox.innerHTML =
    '<div class="emptybox">' + message +
    (detail ? '<br><span style="font-size:12px;color:#9aa3af">' + detail + '</span>' : '') +
    '</div>';
}

/* 极简统计卡：只留「游戏名 + X 天连续」，本月明细塞进 title 悬浮提示 */
function escHtml(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function gameBaseName(g){
  // 只取游戏基础名：去掉「 · 角色昵称」。
  // 后端已经把 game_name / nickname 分开了，这里再兜一层 ——
  // 老存档里的 game_name 可能混进过昵称。
  var raw = String((g && (g.game_name || g.game)) || '');
  var cut = raw.indexOf('·');
  if (cut > 0) raw = raw.slice(0, cut);
  return raw.trim() || String((g && g.game) || '');
}

function renderHistory(d){
  d = d || {};
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = (d.month || '-') + ' 本月';

  // --- 统计卡片：一排大数字（本月明细放 title，鼠标悬停才出现）---
  const statsBox = document.getElementById('h-stats');
  if (statsBox) {
    const games = d.games || [];
    if (!games.length) {
      statsBox.innerHTML = '';
    } else {
      statsBox.innerHTML = games.map(function(g){
        const done = Number(g.month_done || 0);
        const missed = Number(g.month_missed || 0);
        const tip = '本月完成：' + done + ' 天 | 未完成：' + missed + ' 天';
        return '<div class="hstat" title="' + escHtml(tip) + '">' +
          '<div class="name">' + escHtml(gameBaseName(g)) + '</div>' +
          '<div class="big">' + Number(g.streak || 0) + '<small> 天连续</small></div>' +
        '</div>';
      }).join('');
    }
  }

  // --- 7 天明细表 ---
  const tableBox = document.getElementById('h-table');
  if (!tableBox) return;
  const tracked = d.tracked || [];

  // 完全没有历史（data/history.json 还没生成 / 被清空）：给友好空状态，
  // 而不是画一张全是「–」的表格让人以为坏了
  if (!d.has_data) {
    renderHistoryEmpty('暂无历史记录，请等待下一次定时检查写入。',
                       '定时检查跑完一轮后会写入 data/history.json（也可以去「定时与状态」手动触发）。');
    return;
  }

  if (!tracked.length) {
    renderHistoryEmpty('没有启用任何游戏账号。',
                       'config.json 里 accounts 的 enabled 都为 false —— 需要参与检查就改回 true。');
    return;
  }

  const days = d.days || [];
  let html = '<table class="htable"><thead><tr><th style="text-align:left">日期</th>';
  tracked.forEach(function(gid){
    const meta = (d.games || []).find(function(x){ return x.game === gid; });
    html += '<th>' + ((meta && meta.game_name) || gid) + '</th>';
  });
  html += '</tr></thead><tbody>';
  days.forEach(function(row){
    html += '<tr' + (row.is_today ? ' class="today"' : '') + '>' +
      '<td class="date">' + row.label +
        '<span class="wd">' + row.weekday + (row.is_today ? ' · 今天' : '') + '</span></td>';
    tracked.forEach(function(gid){
      html += '<td>' + markOf((row.games || {})[gid]) + '</td>';
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  tableBox.innerHTML = html;
}

function loadHistory(){
  // 分三层处理，别把"渲染出错"和"请求失败"混成同一句红字：
  //   1) 请求/解析失败  -> 页面内空状态（不弹红色 toast）
  //   2) 渲染抛异常     -> 页面内提示 + console 里留堆栈，同样是空状态而不是红条
  //   3) 正常数据       -> 交给 renderHistory
  fetch('/api/history?days=7')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(d){
      try {
        renderHistory(d);
      } catch (e) {
        console.error('渲染历史报表失败', e);
        renderHistoryEmpty('报表渲染出错，请点「刷新」重试。',
                           '详细信息见浏览器控制台（F12）或 logs/dailycheck.log');
      }
    })
    .catch(function(err){
      console.error('读取历史记录失败', err);
      renderHistoryEmpty('暂无历史记录，请等待下一次定时检查写入。',
                         err && err.message ? '（' + err.message + '）' : '');
    });
}

document.getElementById('btn-history-refresh').addEventListener('click', loadHistory);

/* =========================== 启动 =========================== */
const hash = (location.hash || '').replace('#', '');
switchTab(PANEL_NAMES.indexOf(hash) >= 0 ? hash : 'history');
refreshNotice();
loadDevice();
loadThresholds();
loadSchedule();      // 定时页默认折叠，但时间点/开关状态先拉一次，展开时不会闪空
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

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
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

        if parsed.path == "/api/settings":
            self._json(thresholds_status())
            return

        if parsed.path == "/api/autostart":
            from core import autostart

            self._json(autostart.status())
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
