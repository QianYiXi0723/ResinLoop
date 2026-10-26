#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形化扫码登录界面（本地 Web 服务，只监听 127.0.0.1）。

为什么用网页而不是 tkinter：
  - 二维码要足够大才能扫得稳，浏览器缩放/居中天然合适；
  - 只需要标准库 + 已有的 qrcode / pillow，不新增任何依赖；
  - 手机扫码时电脑屏幕不用凑近看。

用法：
    python login_ui.py                  # 启动并自动打开浏览器
    python login_ui.py --port 8800      # 指定端口
    python login_ui.py --no-browser     # 不自动开浏览器

登录成功后直接写 config.json：
    - 米游社：三款游戏共用同一 Cookie，并顺手绑定设备指纹（登录设备 = 查询设备）
    - 森空岛：只更新所选游戏的 token，不碰另一款
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

logger = logging.getLogger("dailycheck.loginui")

MIHOYO_GAMES = ("genshin", "starrail", "zzz")
SKLAND_TARGETS = {"arknights": "明日方舟", "endfield": "明日方舟：终末地"}


# --------------------------------------------------------------------------- #
# 会话状态
# --------------------------------------------------------------------------- #


class Session:
    """一次扫码登录的完整状态。

    state 取值：creating / waiting / scanned / writing / done / error
    """

    def __init__(self, provider: str, target: str = "") -> None:
        self.sid = uuid.uuid4().hex
        self.provider = provider
        self.target = target
        self.state = "creating"
        self.message = "正在准备…"
        self.qr = ""
        self.detail: Dict[str, Any] = {}
        self.created_at = time.time()
        self._lock = threading.Lock()

    def update(self, state: Optional[str] = None, message: Optional[str] = None,
               **detail: Any) -> None:
        with self._lock:
            if state:
                self.state = state
            if message:
                self.message = message
            if detail:
                self.detail.update(detail)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sid": self.sid,
                "provider": self.provider,
                "target": self.target,
                "state": self.state,
                "message": self.message,
                "qr": self.qr,
                "detail": dict(self.detail),
            }


SESSIONS: Dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def _put(session: Session) -> None:
    with SESSIONS_LOCK:
        # 顺手清掉 10 分钟前的旧会话，避免长时间开着页面堆积
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
    """把文本渲染成 PNG 二维码并转 base64；缺依赖时返回空串（前端会降级展示原文）。"""
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


def load_raw_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"找不到 {CONFIG_PATH.name}，请先运行 python main.py --init")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_raw_config(raw: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_mihoyo(cookie: str) -> list[str]:
    """把 Cookie 写进三款米家游戏，并确保查询走真实数据源。"""
    raw = load_raw_config()
    updated: list[str] = []
    for account in raw.get("accounts", []):
        if account.get("game") in MIHOYO_GAMES:
            cred = account.setdefault("credential", {})
            cred["cookie"] = cookie
            # 清掉可能残留的分字段，避免覆盖整串 cookie
            cred.pop("ltuid", None)
            cred.pop("ltoken", None)
            updated.append(str(account.get("nickname") or account.get("game")))
    if not updated:
        raise RuntimeError("config.json 里没有米游社三款游戏的账号节点")

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
    from core.mihoyo_api import bind_device
    from core.mihoyo_login import (
        MiyousheLoginError,
        build_cookie_string,
        create_qr,
        poll_confirm,
    )

    def on_status(status: str) -> None:
        if status == "Scanned":
            session.update("scanned", "已扫码，请在手机上点击「确认登录」")

    try:
        device_id = str(uuid.uuid4()).lower()
        session.update("creating", "正在向米游社申请二维码…")
        qr = create_qr(device_id)
        session.qr = qr_png_base64(qr["url"])
        session.update("waiting", "请用手机「米游社」App 扫码")

        result = poll_confirm(device_id, qr["ticket"], timeout=180, on_status=on_status)

        session.update("writing", "扫码成功，正在整理登录态并绑定设备…")
        cookie, creds = build_cookie_string(result["cookies"])
        bind_device(device_id)
        games = write_mihoyo(cookie)

        session.update(
            "done",
            "登录成功，已写入 config.json",
            ltuid=creds["ltuid"],
            games=games,
            note="三款游戏共用同一设备指纹，可直接运行 python main.py 验证",
        )
        logger.info("米游社登录成功，ltuid=%s，写入 %s", creds["ltuid"], "、".join(games))
    except MiyousheLoginError as exc:
        session.update("error", str(exc))
        logger.warning("米游社登录失败：%s", exc)
    except Exception as exc:  # noqa: BLE001
        session.update("error", f"登录异常：{exc}")
        logger.exception("米游社登录未预期异常")


def run_skland(session: Session) -> None:
    from core.hypergryph_login import (
        HypergryphLoginError,
        create_qr,
        exchange_short_token,
        poll_confirm,
    )
    from core.skland_api import verify_ark_token

    label = SKLAND_TARGETS.get(session.target, session.target)

    def on_status(status: Any) -> None:
        if status == 101:
            session.update("scanned", "已扫码，请在手机上点击「确认」")

    try:
        session.update("creating", "正在向鹰角通行证申请二维码…")
        qr = create_qr()
        session.qr = qr_png_base64(qr["scan_url"])
        session.update("waiting", "请用手机「森空岛」App 扫码（明日方舟 / 终末地通用）")

        scan_code = poll_confirm(qr["scan_id"], timeout=180, on_status=on_status)

        session.update("writing", "扫码成功，正在验证 Token…")
        token = exchange_short_token(scan_code)
        check = verify_ark_token(token)
        if not check.get("ok"):
            raise HypergryphLoginError(f"Token 校验未通过：{check.get('message') or check}")

        name = write_skland(session.target, token)
        session.update(
            "done",
            f"{name} 登录成功，已写入 config.json",
            token_prefix=token[:10] + "…",
            note="该游戏的 cred 缓存已清除，下次运行自动换新",
        )
        logger.info("%s 登录成功", name)
    except HypergryphLoginError as exc:
        session.update("error", str(exc))
        logger.warning("森空岛登录失败：%s", exc)
    except Exception as exc:  # noqa: BLE001
        session.update("error", f"登录异常：{exc}")
        logger.exception("森空岛登录未预期异常")


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>账号登录 · 游戏日常提醒</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --fg:#1a1c1f; --muted:#6b7280;
    --line:#e5e7eb; --brand:#2563eb; --ok:#059669; --warn:#d97706; --err:#dc2626;
    --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06);
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0f1115; --card:#181b21; --fg:#e8eaed; --muted:#9aa3af;
           --line:#272b33; --brand:#5b8def; --ok:#34d399; --warn:#fbbf24; --err:#f87171;
           --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3); }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       padding:32px 16px 64px}
  .wrap{max-width:720px;margin:0 auto}
  header{margin-bottom:24px}
  h1{font-size:22px;margin:0 0 6px;letter-spacing:-.01em}
  .sub{margin:0;color:var(--muted);font-size:14px}
  .cards{display:grid;gap:14px;grid-template-columns:1fr 1fr}
  @media (max-width:560px){ .cards{grid-template-columns:1fr} }
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:18px;box-shadow:var(--shadow)}
  .card-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .card h2{font-size:15px;margin:0}
  .desc{color:var(--muted);font-size:13px;margin:0 0 14px;min-height:38px}
  .dot{width:8px;height:8px;border-radius:50%;flex:none}
  .dot.m{background:#4f9cf9}.dot.s{background:#f0a04b}
  button{width:100%;padding:10px 14px;border-radius:9px;border:1px solid transparent;
         background:var(--brand);color:#fff;font-size:14px;font-weight:500;cursor:pointer;
         transition:opacity .15s}
  button:hover{opacity:.9}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:transparent;border-color:var(--line);color:var(--muted);
               width:auto;padding:8px 14px;margin-top:14px}
  select{width:100%;margin-bottom:10px;padding:8px 10px;border-radius:8px;
         border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:14px}
  .stage{margin-top:22px;background:var(--card);border:1px solid var(--line);
         border-radius:14px;padding:22px;box-shadow:var(--shadow);text-align:center}
  .qrbox{display:inline-flex;padding:12px;background:#fff;border-radius:12px;
         border:1px solid var(--line);min-height:220px;min-width:220px;
         align-items:center;justify-content:center}
  .qrbox img{width:220px;height:220px;display:block;image-rendering:pixelated}
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
  .spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);
        border-top-color:var(--brand);border-radius:50%;animation:s .8s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>账号登录</h1>
    <p class="sub">手机扫码后自动写入 config.json，不用手动复制 Cookie</p>
  </header>

  <div class="cards">
    <section class="card">
      <div class="card-head"><span class="dot m"></span><h2>米游社</h2></div>
      <p class="desc">原神 · 崩坏：星穹铁道 · 绝区零<br>一次扫码，三款同时更新</p>
      <button id="btn-m" onclick="start('mihoyo')">开始扫码</button>
    </section>
    <section class="card">
      <div class="card-head"><span class="dot s"></span><h2>森空岛</h2></div>
      <p class="desc">明日方舟 · 明日方舟：终末地<br>一款一款登录，互不覆盖</p>
      <select id="target">
        <option value="endfield">明日方舟：终末地</option>
        <option value="arknights">明日方舟</option>
      </select>
      <button id="btn-s" onclick="startSkland()">开始扫码</button>
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
</div>

<script>
let sid = null, timer = null;

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
  const [text, cls] = badgeFor(d.state);
  const badge = document.getElementById('badge');
  badge.textContent = text;
  badge.className = 'badge ' + cls;
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
    if (d.detail.token_prefix) items.push('Token：<code>' + d.detail.token_prefix + '</code>');
    if (d.detail.note) items.push(d.detail.note);
  }
  kv.innerHTML = items.join('<br>');
  kv.hidden = items.length === 0;
}

async function start(provider, target){
  reset();
  setBusy(true);
  const q = '/api/start?provider=' + encodeURIComponent(provider)
          + (target ? '&target=' + encodeURIComponent(target) : '');
  try {
    const r = await fetch(q);
    const d = await r.json();
    if (!d.ok) { showError(d.message); setBusy(false); return; }
    sid = d.sid;
    poll();
  } catch (e) {
    showError('无法连接本地服务：' + e);
    setBusy(false);
  }
}

function startSkland(){
  const t = document.getElementById('target').value;
  start('skland', t);
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
    .then(r => r.json())
    .then(d => {
      render(d);
      if (d.state === 'done' || d.state === 'error') {
        setBusy(false);
        return;
      }
      timer = setTimeout(poll, 1000);
    })
    .catch(() => { timer = setTimeout(poll, 2000); });
}

function reset(){
  if (timer) { clearTimeout(timer); timer = null; }
  sid = null;
  setBusy(false);
  document.getElementById('stage').hidden = true;
  document.getElementById('qrbox').innerHTML = '<span class="ph">二维码准备中…</span>';
  document.getElementById('detail').hidden = true;
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyCheckLogin/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 只服务本机，禁止被外站嵌入
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
            target = (query.get("target") or [""])[0]
            if provider == "mihoyo":
                session = Session("mihoyo")
                _put(session)
                threading.Thread(target=run_mihoyo, args=(session,), daemon=True).start()
                self._json({"ok": True, "sid": session.sid})
                return
            if provider == "skland":
                if target not in SKLAND_TARGETS:
                    self._json({"ok": False, "message": "请选择要登录的游戏"}, 400)
                    return
                session = Session("skland", target)
                _put(session)
                threading.Thread(target=run_skland, args=(session,), daemon=True).start()
                self._json({"ok": True, "sid": session.sid})
                return
            self._json({"ok": False, "message": "未知的 provider"}, 400)
            return

        if parsed.path == "/api/state":
            sid = (query.get("sid") or [""])[0]
            session = _get(sid)
            if session is None:
                self._json({"state": "error", "message": "会话已失效，请重新开始"}, 404)
                return
            self._json(session.snapshot())
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def pick_port(preferred: int, tries: int = 12) -> int:
    """从 preferred 开始找一个能绑定的端口。"""
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{preferred}~{preferred + tries - 1} 端口都被占用")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="图形化扫码登录（本地网页）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not CONFIG_PATH.exists():
        print(f"找不到 {CONFIG_PATH}")
        print("请先运行：python main.py --init")
        return 1

    try:
        port = pick_port(args.port)
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 58)
    print("  账号登录界面已启动")
    print(f"  地址：{url}")
    print("  按 Ctrl+C 结束")
    print("=" * 58)

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
