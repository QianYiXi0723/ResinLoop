# -*- coding: utf-8 -*-
"""深度自动化测试（Python 侧）：测试 1 DOM 渲染与初始化 + 测试 3 后端 API 与暂停真伪。

测试 1 在本进程内起一个临时实例（127.0.0.1:8766，lan_tools=False 零副作用），
用 requests 请求真实页面做静态断言；测试 3 打用户真实实例（8765）做守护进程
全生命周期（基线停止 → stop → 验真死 → start → 验真活 → stop 回基线）。
"""
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

BASE = "http://127.0.0.1:8765"      # 用户真实实例：守护进程生命周期测试
TMP_PORT = 8766                     # 测试 1 临时实例端口（同份代码、独立端口）

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

session = requests.Session()
session.trust_env = False   # 本机系统代理会拦 localhost，必须关

def wait_daemon(expect_running, timeout=8.0):
    """守护进程起停是异步的：轮询 /api/schedule 直到 running 达到期望或超时。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = session.get(BASE + "/api/schedule", timeout=8).json().get("daemon", {})
        if bool(last.get("running")) is bool(expect_running):
            return last
        time.sleep(0.5)
    return last

# ================= 测试 3：后端 API 与暂停真伪 =================
print("== 测试 3：后端 API 与暂停真伪（打真实实例 8765） ==")
r = session.get(BASE + "/api/schedule", timeout=8)
d = r.json()
check("T3.1 GET /api/schedule 可达且 ok", r.status_code == 200 and d.get("ok") is True)
baseline = d.get("daemon", {})
base_running = baseline.get("running")
print("      基线: " + json.dumps({k: baseline.get(k) for k in ("running", "pid", "alive", "started_at")}, ensure_ascii=False))

# ---- 停止（收敛到停止态；基线本就是停止，这里验证幂等 no-op） ----
r = session.post(BASE + "/api/daemon/stop", json={}, timeout=15)
d2 = r.json()
check("T3.2 POST /api/daemon/stop → HTTP 200", r.status_code == 200, r.text[:120])
check("T3.3 stop 接口自报 ok（未运行时 already=True）",
      d2.get("ok") is True, d2.get("message", ""))

time.sleep(0.5)
d3 = wait_daemon(False)
check("T3.4 停止后后端真身 running=False",
      d3.get("running") is False,
      json.dumps({k: d3.get(k) for k in ("running", "pid", "alive")}, ensure_ascii=False))
check("T3.5 停止后后端真身 alive=False 且 pid=None",
      d3.get("alive") is False and d3.get("pid") is None)

# ---- 启动：反向证明不是"前端装死"——真启也必须真活 ----
r = session.post(BASE + "/api/daemon/start", json={}, timeout=15)
d4 = r.json()
check("T3.6 POST /api/daemon/start → ok",
      r.status_code == 200 and d4.get("ok") is True, d4.get("message", ""))

d5 = wait_daemon(True)
check("T3.7 启动后真身 running=True 且有真实 pid/started_at",
      d5.get("running") is True and isinstance(d5.get("pid"), int) and bool(d5.get("started_at")),
      json.dumps({k: d5.get(k) for k in ("running", "pid", "alive", "started_at")}, ensure_ascii=False))
check("T3.8 心跳在跳（heartbeat_age 为非负数）",
      isinstance(d5.get("heartbeat_age"), (int, float)) and d5["heartbeat_age"] >= 0,
      "heartbeat_age=" + str(d5.get("heartbeat_age")))

# ---- 核心断言：再停一次，确认进程真的没了（不是前端装死） ----
r = session.post(BASE + "/api/daemon/stop", json={}, timeout=15)
time.sleep(0.5)
d6 = wait_daemon(False)
check("T3.9 再次 stop 后 running=False / alive=False / pid=None（真停）",
      d6.get("running") is False and d6.get("alive") is False and d6.get("pid") is None,
      json.dumps({k: d6.get(k) for k in ("running", "pid", "alive")}, ensure_ascii=False))
print("      结束态: " + json.dumps({k: d6.get(k) for k in ("running", "pid", "alive")}, ensure_ascii=False)
      + "（基线 running=" + str(base_running) + "）")

# ================= 测试 1：DOM 渲染与初始化（临时实例 8766） =================
print("== 测试 1：DOM 渲染与初始化（临时服务 127.0.0.1:%d） ==" % TMP_PORT)
import login_ui
info = login_ui.serve(port=TMP_PORT, lan_tools=False)   # 后台线程，零副作用
time.sleep(1.2)
try:
    r = requests.get("http://127.0.0.1:%d/" % info["port"], timeout=8,
                     proxies={"http": None, "https": None})
    html = r.text
    check("T1.1 临时服务返回页面 200 且含 ResinLoop",
          r.status_code == 200 and "ResinLoop" in html)

    # ---- 首屏静态断言：圆点 border-radius / 各网格 grid-template-columns ----
    check("T1.2 圆点 .dot 含 border-radius:50%",
          ".dot{width:8px;height:8px;border-radius:50%" in html)
    check("T1.3 主网格 12 列 grid-template-columns:repeat(12,1fr)",
          "grid-template-columns:repeat(12,1fr)" in html)
    check("T1.4 KPI 摘要条网格 repeat(4,1fr)",
          "grid-template-columns:repeat(4,1fr)" in html)
    check("T1.5 活动日历 auto-fill 网格",
          "repeat(auto-fill, minmax(min(320px, 100%), 1fr))" in html)

    # ---- JS 初始化调用断言（本项目真实函数名：refreshSetSummaries / renderDaemon+tickHeartbeat） ----
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    js = m.group(1) if m else ""
    i0 = js.find("(function bindSetSummaries")
    iife = js[i0:js.find("})();", i0)] if i0 >= 0 else ""
    check("T1.6 初始化真的调用了摘要刷新 refreshSetSummaries（IIFE 内首屏即调）",
          "refreshSetSummaries();" in iife)
    r0 = js.find("function renderDaemon")
    rd = js[r0:js.find("function shortStamp")] if r0 >= 0 else ""
    check("T1.7 圆点状态更新存在：renderDaemon 内按 running 切换 dot.className",
          "dot.className = 'dot' + (running ? ' on' : '')" in rd)
    check("T1.8 心跳刷新存在：renderDaemon 立即调用 tickHeartbeat()",
          "tickHeartbeat();" in rd)
finally:
    login_ui.stop_server(info)

passed = sum(1 for _, ok in results if ok)
print("\n[Python 侧结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
