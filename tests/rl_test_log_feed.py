# -*- coding: utf-8 -*-
"""底部日志「用户事件流」离线验证（方案 A，临时目录零网络）。

背景（2026-10-31 用户主诉 + 实测量化）：
    底部运行日志收起条取"最新任意行"，实测噪音占比 ~75-80%
    （守护进程生命周期 / provider 切换 / 凭证重换等机械 INFO 行），
    用户无法从里面读到"巡检跑没跑、推送发了没、出了什么错"。
    方案 A：后端给每块日志打 level + noise（黑名单规则表），
    收起条 usefil=1 取最新非噪音块，展开视图用户视图折叠噪音。

覆盖：
  D1  分级：ERROR/WARNING/INFO 正确落级
  D2  噪音规则：生命周期/机械行标噪；结果行与 WARNING/ERROR 不标
  D3  useful=1：最新行为噪音时向前找最新非噪音块；全噪音回退最新块
  D4  续行合并：traceback 多行块保持完整且继承时间戳
  D5  双源合并：dailycheck+desktop 交错行按时间戳排序
  D6  响应兼容：lines 恒在（旧消费方不炸）；events.level 正确
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import login_ui  # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="rl-logfeed-"))
log_dir = tmpdir / "logs"
log_dir.mkdir()
login_ui.BASE_DIR = tmpdir   # log_tail 读 BASE_DIR/logs，单常量重定向即隔离

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


# 夹具：覆盖全部噪音形态 + 结果行 + 警告 + 错误 + traceback 续行
DAEMON_LOG = "\n".join([
    "2026-10-31 09:00:00 [INFO   ] dailycheck.daemon: 守护进程启动｜PID=100｜间隔=30s",
    "2026-10-31 09:00:00 [INFO   ] dailycheck.daemon: 时间点：13:00、22:00、23:59｜下次：2026-10-31 13:00",
    "2026-10-31 09:00:00 [INFO   ] dailycheck.daemon: 轮询线程已启动，间隔 30 秒",
    "2026-10-31 13:00:12 [INFO   ] dailycheck.daemon: 命中时间点 13:00，开始检查",
    "2026-10-31 13:00:40 [ERROR  ] dailycheck.skland: 森空岛接口异常（HTTP 500）",
    "2026-10-31 13:00:41 [WARNING] dailycheck.main: 明日方舟查询失败，下一轮重试",
    "2026-10-31 20:30:10 [INFO   ] dailycheck.daemon: 命中时间点 20:30，开始检查",
    "2026-10-31 20:30:55 [INFO   ] dailycheck.main: 崩坏：星穹铁道(开拓者) -> 未完成 1 项",
    "2026-10-31 20:30:55 [INFO   ] dailycheck.main: 明日方舟(KiloDusk#0642) -> 全部完成",
])
DESKTOP_LOG = "\n".join([
    "2026-10-31 09:48:21 [INFO   ] dailycheck.querier: provider=miyoushe 未覆盖游戏 endfield，自动改用 skland 数据源",
    "2026-10-31 09:48:24 [INFO   ] dailycheck.skland: 强制重新换取森空岛凭证（skland_cred_endfield.json）",
    "2026-10-31 09:48:45 [INFO   ] dailycheck.app: 窗口已关闭",
    "2026-10-31 10:12:03 [INFO   ] dailycheck.notifier: [serverchan] 推送成功",
    "2026-10-31 10:12:03 [ERROR  ] dailycheck.notifier: [qmsg] 推送失败：HTTP 502",
    "2026-10-31 10:13:00 [ERROR  ] dailycheck.main: 巡检过程异常\nTraceback (most recent call last):\n  File \"main.py\", line 1, in <module>\nValueError: boom",
])
(log_dir / "dailycheck.log").write_text(DAEMON_LOG + "\n", encoding="utf-8")
(log_dir / "desktop.log").write_text(DESKTOP_LOG + "\n", encoding="utf-8")

full = login_ui.log_tail(200)
events = full.get("events") or []

# ===================== D1 分级 =====================
print("== D1 分级 ==")
levels = {e["text"][:19]: e["level"] for e in events}
err_line = [e for e in events if "森空岛接口异常" in e["text"]]
warn_line = [e for e in events if "明日方舟查询失败" in e["text"]]
check("D1a ERROR → error", err_line and err_line[0]["level"] == "error")
check("D1b WARNING → warn", warn_line and warn_line[0]["level"] == "warn")

# ===================== D2 噪音规则 =====================
print("== D2 噪音规则 ==")
noise_texts = [e["text"] for e in events if e["noise"]]
keep_texts = [e["text"] for e in events if not e["noise"]]
check("D2a 生命周期行标噪（守护进程启动）",
      any("守护进程启动" in t for t in noise_texts), len(noise_texts))
check("D2b provider 切换行标噪", any("未覆盖游戏" in t for t in noise_texts))
check("D2c 凭证重换行标噪", any("强制重新换取" in t for t in noise_texts))
check("D2d 结果行不标噪（星铁未完成）",
      any("崩坏：星穹铁道" in t and "未完成" in t for t in keep_texts))
check("D2e WARNING/ERROR 永不标噪",
      not any("森空岛接口异常" in t for t in noise_texts)
      and not any("明日方舟查询失败" in t for t in noise_texts))

# ===================== D3 useful=1 选针 =====================
print("== D3 useful=1：噪音包围中挑最新非噪音 ==")
# 日志末尾（20:30:55 的两行结果之后）没有更新内容 → 最新非噪音=20:30 的结果行
# 为验证"最新行是噪音时向前找"，往 desktop.log 追加一行最新噪音再读
with (log_dir / "desktop.log").open("a", encoding="utf-8") as f:
    f.write("2026-10-31 21:00:00 [INFO   ] dailycheck.app: 窗口已关闭\n")
picked = login_ui.log_tail(60, useful=True)
picked_text = (picked.get("lines") or [""])[0]
check("D3a 最新行为噪音时取向前找（拿到 20:30 结果行而非窗口关闭）",
      "崩坏：星穹铁道" in picked_text or "明日方舟" in picked_text, picked_text)
check("D3b picked 块带 level 且非噪音",
      bool(picked.get("events")) and picked["events"][0]["noise"] is False,
      picked.get("events"))
# 全噪音日志 → 回退最新块（两个源都灌满噪音，排除另一源提供非噪音行）
all_noise = "\n".join([
    "2026-11-01 09:00:00 [INFO   ] dailycheck.daemon: 守护进程启动｜PID=2",
    "2026-11-01 09:00:00 [INFO   ] dailycheck.daemon: 轮询线程已启动",
])
(log_dir / "dailycheck.log").write_text(all_noise + "\n", encoding="utf-8")
(log_dir / "desktop.log").write_text(
    "2026-11-01 09:00:05 [INFO   ] dailycheck.app: 窗口已关闭\n", encoding="utf-8")
fallback = login_ui.log_tail(60, useful=True)
check("D3c 全噪音时回退最新块（不空手）",
      bool((fallback.get("lines") or [])) and "窗口已关闭" in fallback["lines"][0],
      fallback.get("lines"))
# 恢复夹具（后续断言还要用完整双源）
(log_dir / "dailycheck.log").write_text(DAEMON_LOG + "\n", encoding="utf-8")
(log_dir / "desktop.log").write_text(DESKTOP_LOG + "\n", encoding="utf-8")

# ===================== D4 续行合并 =====================
print("== D4 traceback 续行并入块 ==")
tb = [e for e in events if "巡检过程异常" in e["text"]]
check("D4a traceback 整块（含 ValueError 行）",
      tb and "ValueError: boom" in tb[0]["text"] and "Traceback" in tb[0]["text"])
check("D4b 块级 error", tb and tb[0]["level"] == "error")

# ===================== D5 双源排序 =====================
print("== D5 双源时间戳排序 ==")
all_ts = [e["ts"] for e in events]
check("D5a 事件按时间戳升序", all_ts == sorted(all_ts))

# ===================== D6 响应兼容 =====================
print("== D6 响应兼容 ==")
check("D6a lines 恒在（旧消费方不炸）",
      isinstance(full.get("lines"), list) and len(full["lines"]) == len(events))
check("D6b events 带 ts/level/noise/text 四键",
      events and all(set(e) >= {"ts", "level", "noise", "text"} for e in events))
strip = login_ui.log_tail(60, useful=True)
check("D6c useful 响应同样带 lines+events（收起条/展开共用客户端）",
      isinstance(strip.get("lines"), list) and isinstance(strip.get("events"), list))

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
