# -*- coding: utf-8 -*-
"""便携引擎（engine/）独立存活闭环验收（全自动，不碰生产实例 8765）。

验证项：
  V0 清场        8899 端口上的旧实例全部杀净
  V1 环境隔离    精简环境变量 + PYTHONPATH/PYTHONHOME **投毒**（指向假 requests.py）
                 启动引擎 → 必须存活且无视毒源（Embeddable ._pth 隔离模式）
  V2 依赖完整    requests/qrcode/PIL 全部来自 engine 侧 site-packages；
                 系统专属依赖 pystray/webview 必须 ImportError（证明没有环境泄漏）
  V3 单实例守卫  第二实例必须检测端口占用并自待命退出（贴 engine.log 原文），
                 第一实例继续 HTTP 200
  V4 API 冒烟    /api/env + /api/schedule 真实 JSON
  V5 清理现场    进程杀净、端口释放、连接拒绝

运行：python tests/rl_test_engine_standalone.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
PY = ENGINE / "python" / "python.exe"
PYW = ENGINE / "python" / "pythonw.exe"
APP = ENGINE / "app.py"
LOG = ENGINE / "logs" / "engine.log"
PORT = 8899
BASE_URL = f"http://127.0.0.1:{PORT}"
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}  {detail}")


def http_get(url: str, timeout: float = 5.0):
    """绕过系统代理直连 127.0.0.1（本机代理拦 localhost 的老坑）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.URLError as exc:
        return None, str(exc.reason).encode()


def kill_port(port: int):
    # netstat 输出是 GBK（"活动连接"表头），按 utf-8 解会炸——GBK 陷阱
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, encoding="gbk", errors="replace").stdout or ""
    pids = {ln.split()[-1] for ln in out.splitlines()
            if f":{port}" in ln and "LISTENING" in ln}
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    return pids


# ---------------------------------------------------------------- V0 清场
old = kill_port(PORT)
print(f"V0 清场：8899 上的旧实例 {old or '无'}")
time.sleep(1)

# ---------------------------------------------------------------- V1 环境隔离
poison_dir = ROOT / "build_wheels" / "evil_shadow"
poison_dir.mkdir(parents=True, exist_ok=True)
(poison_dir / "requests.py").write_text(
    "raise RuntimeError('EVIL SHADOW requests.py 被加载了！')", encoding="utf-8")

# 精简环境变量：只留 Windows 必需项；PYTHONPATH/PYTHONHOME 指向毒源
clean_env = {
    k: v for k, v in os.environ.items()
    if k in ("SYSTEMROOT", "SystemDrive", "TEMP", "TMP", "PATH", "COMSPEC",
             "PATHEXT", "USERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "COMPUTERNAME", "PROCESSOR_ARCHITECTURE")
}
clean_env["PYTHONPATH"] = str(poison_dir)          # 投毒
clean_env["PYTHONHOME"] = str(ROOT / "build_wheels" / "nonexistent_python")

proc = subprocess.Popen(
    [str(PYW), str(APP), "--port", str(PORT), "--no-lan-tools"],
    cwd=str(ROOT), env=clean_env, creationflags=DETACHED,
)
print(f"V1 启动引擎 pid={proc.pid}（PYTHONPATH 已投毒 → {poison_dir}）")
time.sleep(6)
check("V1a 引擎进程存活（poll=None）", proc.poll() is None, str(proc.poll()))
status, body = http_get(BASE_URL + "/")
check("V1b 页面 HTTP 200", status == 200, f"status={status}")
check("V1c 页面体积完整（>150KB）", body is not None and len(body) > 150_000,
      str(len(body) if body else body))

# ---------------------------------------------------------------- V2 依赖完整性
probe = (
    "import json, sys, os\n"
    "info = {'exe': sys.executable, 'isolated': bool(sys.flags.isolated)}\n"
    "import requests, qrcode, PIL\n"
    "info['requests'] = requests.__file__\n"
    "info['qrcode'] = qrcode.__file__\n"
    "info['PIL'] = PIL.__file__\n"
    "from importlib.metadata import version as _mdver\n"
    "info['versions'] = [requests.__version__, _mdver('qrcode'), PIL.__version__]\n"
    "for mod in ('pystray', 'webview'):\n"
    "    try:\n"
    "        __import__(mod)\n"
    "        info[mod] = 'IMPORTED(不应发生!)'\n"
    "    except ImportError:\n"
    "        info[mod] = 'ImportError: 良好'\n"
    "img = qrcode.make('standalone-test')\n"
    "info['qr'] = list(img.size)\n"
    "info['syspath_has_poison'] = any('evil_shadow' in p for p in sys.path)\n"
    "info['PYTHONPATH_env'] = os.environ.get('PYTHONPATH', '')\n"
    "print(json.dumps(info, ensure_ascii=True))\n"
)
r = subprocess.run([str(PY), "-c", probe], env=clean_env,
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=60)
try:
    info = json.loads(r.stdout.strip().splitlines()[-1])
except Exception as exc:  # noqa: BLE001
    print("V2 探针输出解析失败：", r.stdout[-500:], r.stderr[-500:])
    info = {}

check("V2a 解释器是 engine 侧", str(ENGINE) in info.get("exe", ""), info.get("exe", ""))
for mod in ("requests", "qrcode", "PIL"):
    check(f"V2b {mod} 来自 engine site-packages",
          str(ENGINE / "python" / "Lib" / "site-packages") in info.get(mod, ""),
          info.get(mod, ""))
check("V2c 系统专属依赖 pystray 已隔离", "ImportError" in info.get("pystray", ""),
      info.get("pystray", ""))
check("V2d 系统专属依赖 webview 已隔离", "ImportError" in info.get("webview", ""),
      info.get("webview", ""))
check("V2e 毒源未进入 sys.path", info.get("syspath_has_poison") is False,
      str(info.get("syspath_has_poison")))
check("V2f 二维码可渲染", info.get("qr") == [330, 330], str(info.get("qr")))
print("V2 明细:", json.dumps({k: info.get(k) for k in
      ("exe", "versions", "PYTHONPATH_env", "isolated")}, ensure_ascii=False))

# ---------------------------------------------------------------- V3 单实例守卫
p2 = subprocess.Popen(
    [str(PYW), str(APP), "--port", str(PORT), "--no-lan-tools"],
    cwd=str(ROOT), env=clean_env, creationflags=DETACHED,
)
time.sleep(4)
check("V3a 第二实例已自退出", p2.poll() is not None,
      f"returncode={p2.poll()}")
check("V3b 第二实例退出码 0（待命退出非崩溃）", p2.poll() == 0,
      str(p2.poll()))
log_tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
guard_lines = [ln for ln in log_tail if "已有控制台实例" in ln]
check("V3c engine.log 有守卫日志", bool(guard_lines), str(log_tail[-3:]))
for ln in guard_lines[-1:]:
    print("V3 引擎日志原文:", ln)
status, body = http_get(BASE_URL + "/")
check("V3d 第一实例仍 HTTP 200", status == 200 and len(body or b"") > 150_000,
      f"status={status} size={len(body or b'')}")

# ---------------------------------------------------------------- V4 API 冒烟
status, body = http_get(BASE_URL + "/api/env")
env = json.loads(body) if status == 200 else {}
check("V4a /api/env 200 且含 channels", status == 200 and "channels" in env,
      f"status={status} {str(env)[:80]}")
print("V4 /api/env:", json.dumps(env, ensure_ascii=False)[:160])
status, body = http_get(BASE_URL + "/api/schedule")
sched = json.loads(body) if status == 200 else {}
check("V4b /api/schedule 200 且含 daemon/times",
      status == 200 and "daemon" in sched and "times" in sched,
      f"status={status} keys={sorted(sched)[:8]}")
print("V4 /api/schedule:", json.dumps(
    {k: sched.get(k) for k in ("ok", "times", "next_run")}, ensure_ascii=False))

# ---------------------------------------------------------------- V5 清理现场
killed = kill_port(PORT)
time.sleep(1)
print(f"V5 已杀净端口进程：{killed or '无'}")
status, body = http_get(BASE_URL + "/", timeout=3)
check("V5a 8899 已释放（连接被拒）", status is None, f"status={status} {body[:60] if status is None else ''}")
out = subprocess.run(["netstat", "-ano"], capture_output=True,
                     text=True, encoding="gbk", errors="replace").stdout or ""
check("V5b netstat 无 8899 LISTENING",
      not any(":8899" in ln and "LISTENING" in ln for ln in out.splitlines()))
(poison_dir / "requests.py").unlink()
print(f"\n=== 便携引擎独立存活验收: {PASS} PASS / {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
