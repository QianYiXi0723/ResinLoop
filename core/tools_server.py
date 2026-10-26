# -*- coding: utf-8 -*-
"""局域网只读文件服务 —— 只为让手机扫码下载 tools/ 里的 APK。

为什么单独起一个端口，而不是把主控制台开到 0.0.0.0：
    主控制台有 /api/push（读推送 Key）、/api/daemon/*（启停进程）等接口，
    一旦暴露到局域网，同一个 WiFi 下的任何人都能调。
    这里只做一件事：把 tools/ 目录下一层的文件以只读方式发出去。

安全边界：
    - 只服务 tools/ 下的**普通文件**，不接受子目录、不接受 ``..``；
    - 只支持 GET / HEAD；
    - 目录里没有敏感信息（只有 APK 与说明文件）。
"""

from __future__ import annotations

import logging
import mimetypes
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger("dailycheck.tools")

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
DEFAULT_PORT = 8790


def lan_ip() -> str:
    """取本机在局域网里的地址（UDP connect 不发包，只用来选路）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.5)
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def list_files() -> List[Dict[str, Any]]:
    if not TOOLS_DIR.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for path in sorted(TOOLS_DIR.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            items.append({"name": path.name, "size": path.stat().st_size})
    return items


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class _Handler(BaseHTTPRequestHandler):
    server_version = "ResinLoopTools/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- 响应助手 --------------------------------------------------------- #

    def _send_bytes(self, code: int, body: bytes, ctype: str,
                    extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _text(self, code: int, text: str) -> None:
        self._send_bytes(code, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _index(self) -> None:
        files = list_files()
        if not files:
            self._text(404, "tools/ 目录里还没有文件")
            return
        rows = "".join(
            f'<li><a href="/{quote(item["name"])}">{item["name"]}</a>'
            f'<span>{_human_size(item["size"])}</span></li>'
            for item in files
        )
        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>下载</title><style>
body{{font:16px/1.7 -apple-system,"Microsoft YaHei",sans-serif;margin:0;padding:24px;
background:#f6f7f9;color:#1a1c1f}}
h1{{font-size:18px;margin:0 0 4px}}p{{color:#6b7280;font-size:13px;margin:0 0 18px}}
ul{{list-style:none;padding:0;margin:0}}
li{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px;
margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}}
a{{color:#2563eb;text-decoration:none;font-weight:500}}
span{{color:#9aa3af;font-size:12px}}
</style></head><body><h1>下载文件</h1><p>点文件名即可下载到手机</p>
<ul>{rows}</ul></body></html>"""
        self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")

    # -- GET / HEAD ------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        name = path.lstrip("/")

        if not name:
            self._index()
            return
        if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
            self._text(403, "forbidden")
            return

        target = TOOLS_DIR / name
        if not target.is_file():
            self._text(404, "not found")
            return

        try:
            data = target.read_bytes()
        except OSError as exc:
            self._text(500, f"读取失败：{exc}")
            return

        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.lower().endswith(".apk"):
            ctype = "application/vnd.android.package-archive"
        self._send_bytes(
            200,
            data,
            ctype,
            {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "Cache-Control": "no-store",
            },
        )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


class ToolsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # Windows 上必须关掉，否则会重复绑定


#: 首选端口被占时，往后最多试几个（8790、8791、…）
PORT_TRIES = 6

#: 最近一次启动失败的原因；成功时清空。界面拿它显示给用户，别再只说"未启动"
LAST_ERROR: str = ""


def _bind(candidate: int) -> Optional[ToolsServer]:
    """尝试绑定一个端口；失败返回 None（不抛异常）。"""
    try:
        server = ToolsServer(("0.0.0.0", candidate), _Handler)
    except OSError as exc:
        logger.debug("绑定 0.0.0.0:%d 失败：%s", candidate, exc)
        return None
    return server


def _launch(server: ToolsServer, port: int) -> Dict[str, Any]:
    thread = threading.Thread(
        target=server.serve_forever, name=f"tools-http-{port}", daemon=True
    )
    thread.start()
    ip = lan_ip()
    logger.info("APK 下载服务已启动：http://%s:%d/", ip, port)
    return {"server": server, "port": port, "ip": ip,
            "url": f"http://{ip}:{port}/"}


def start(port: int = DEFAULT_PORT, tries: int = PORT_TRIES) -> Optional[Dict[str, Any]]:
    """在 0.0.0.0 上起一个只读文件服务；失败时把原因写进 :data:`LAST_ERROR`。

    端口策略（不会再因为 8790~8795 全被占就彻底放弃）：

    1. 先按 ``port`` 往后试 ``tries`` 个，方便用户记住固定端口；
    2. 全被占用则让操作系统分配一个空闲端口（``port=0``）—— 二维码里的
       地址会自动跟着变，手机扫码下载不受影响；
    3. 连系统端口都绑不上（防火墙 / 权限），才返回 None 并记下原因。
    """
    global LAST_ERROR

    if not TOOLS_DIR.is_dir():
        # 目录不存在就建一个，这样用户把 APK 丢进去、点一下重试就能用
        try:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            logger.info("已创建 %s（原来不存在）", TOOLS_DIR)
        except OSError as exc:
            LAST_ERROR = f"无法创建 tools/ 目录：{exc}"
            logger.warning("APK 下载服务启动失败：%s", LAST_ERROR)
            return None

    for offset in range(tries):
        candidate = port + offset
        server = _bind(candidate)
        if server is not None:
            LAST_ERROR = ""
            return _launch(server, candidate)

    # 固定端口段全被占 —— 改用系统分配的空闲端口
    fallback = _bind(0)
    if fallback is not None:
        actual = fallback.server_address[1]
        LAST_ERROR = ""
        logger.info("首选端口 %d~%d 都被占用，改用系统分配的 %d",
                    port, port + tries - 1, actual)
        return _launch(fallback, actual)

    LAST_ERROR = (f"端口 {port}~{port + tries - 1} 都被占用，"
                  f"且无法让系统分配空闲端口（可能被防火墙/安全软件拦截）")
    logger.warning("APK 下载服务启动失败：%s", LAST_ERROR)
    return None


def stop(info: Optional[Dict[str, Any]]) -> None:
    """关掉 :func:`start` 起出来的服务（幂等，可重复调用）。"""
    server = (info or {}).get("server")
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        logger.debug("关闭 APK 下载服务时出错（已忽略）", exc_info=True)
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        logger.debug("释放 APK 下载服务端口时出错（已忽略）", exc_info=True)
