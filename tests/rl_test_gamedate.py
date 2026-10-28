# -*- coding: utf-8 -*-
"""游戏日期（04:00 日界）自动化验证。

覆盖：
  T1  任务4核心场景：模拟系统时间 2026-10-29 02:30 执行检查写入 → history 必须记 2026-10-28
  T2  日界四点扫描：03:59→昨天 / 04:00→今天 / 23:59→今天 / 00:00→昨天
  T3  默认路径（不传 at，等价 main.py 真实调用）：patch 模块时钟后同样归属昨天
  T4  前端对齐（任务2）：summary() 的 today/is_today 在凌晨 02:30 指向前一天，
      且 02:30 的完成数据落在"今天"（游戏日期）行 —— KPI/热力图零改动即对齐
  T5  跨 0 点补发（任务3）：daemon.pending_slots 在 00:30 能补昨天 23:59 的班，
      槽位键记昨天；已补过不重跑；超出窗口/白天的班次不受影响
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import history  # noqa: E402

# ---- 隔离：history 读写全部重定向到临时目录，绝不碰真实 data/history.json ----
tmpdir = Path(tempfile.mkdtemp(prefix="rl-gamedate-"))
history.DATA_DIR = tmpdir
history.HISTORY_PATH = tmpdir / "history.json"

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

def fake_report(at, game="genshin", finished=True):
    return SimpleNamespace(
        statuses=[SimpleNamespace(game_id=game, game_name="原神", nickname="旅行者",
                                  ok=True, finished=finished, unfinished_tasks=[])],
        generated_at=at,
    )

def reset():
    history._atomic_write(history._empty())

# ================= T1 任务4核心场景 =================
print("== T1 任务4核心场景：10-29 02:30 写入 → 必须记 10-28 ==")
reset()
at = datetime(2026, 10, 29, 2, 30, 0)
data = history.record(fake_report(at), at=at)
days = data.get("days", {})
check("T1.1 写入键为 2026-10-28", "2026-10-28" in days, sorted(days.keys()))
check("T1.2 严禁出现 2026-10-29", "2026-10-29" not in days, sorted(days.keys()))
check("T1.3 完成状态粘住（finished=True）",
      (days.get("2026-10-28", {}).get("genshin", {}) or {}).get("finished") is True)
check("T1.4 last 时间保留真实时刻 02:30",
      (days.get("2026-10-28", {}).get("genshin", {}) or {}).get("last") == "02:30")

# ================= T2 日界四点扫描 =================
print("== T2 日界四点扫描 ==")
cases = [
    (datetime(2026, 10, 29, 3, 59, 59), "2026-10-28"),
    (datetime(2026, 10, 29, 4, 0, 0),   "2026-10-29"),
    (datetime(2026, 10, 29, 23, 59, 0), "2026-10-29"),
    (datetime(2026, 10, 29, 0, 0, 0),   "2026-10-28"),
]
for i, (at_i, want) in enumerate(cases, 1):
    check("T2.%d %s → %s" % (i, at_i.strftime("%m-%d %H:%M:%S"), want),
          history.game_date_key(at_i) == want, history.game_date_key(at_i))

# ================= T3 默认路径（main.py 真实调用形态） =================
print("== T3 默认路径：patch 模块时钟到 02:30，record(report) 不传 at ==")
reset()
real_datetime = history.datetime
class FakeDateTime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 29, 2, 30, 0)
history.datetime = FakeDateTime
try:
    data = history.record(fake_report(datetime(2026, 10, 29, 2, 30, 0)))  # 不传 at
    days = data.get("days", {})
    check("T3.1 默认路径写入键为 2026-10-28", "2026-10-28" in days, sorted(days.keys()))
    check("T3.2 默认路径不产生 2026-10-29", "2026-10-29" not in days, sorted(days.keys()))
finally:
    history.datetime = real_datetime

# ================= T4 前端对齐（KPI/热力图的「今天」） =================
print("== T4 summary() 的「今天」与游戏日期对齐（02:30 场景） ==")
history.datetime = FakeDateTime
try:
    s = history.summary(days=7, enabled_games=["genshin"])
    check("T4.1 summary.today = 2026-10-28", s.get("today") == "2026-10-28", s.get("today"))
    # 列表按旧→新排列（页面 10/22 在上、今天在最下），游戏今天 = 最后一行
    rows = s.get("days") or []
    top = rows[-1] if rows else {}
    check("T4.2 报表最新一行就是游戏今天（10-28）且 is_today=True",
          top.get("date") == "2026-10-28" and top.get("is_today") is True,
          {"date": top.get("date"), "is_today": top.get("is_today")})
    check("T4.3 不存在 is_today 挂在 10-29 上的行",
          not any(row.get("is_today") and row.get("date") == "2026-10-29" for row in rows))
    cell = (top.get("games") or {}).get("genshin", {})
    check("T4.4 凌晨 02:30 的完成落在游戏今天行（KPI 今日完成=1/1 不被 0 点清空）",
          cell.get("known") is True and cell.get("finished") is True, cell)
    check("T4.5 月份口径 = 游戏日期的月份", s.get("month") == "2026-10", s.get("month"))
finally:
    history.datetime = real_datetime

# ================= T5 跨 0 点补发（daemon.pending_slots） =================
print("== T5 跨 0 点补发：00:30 补昨天 23:59 的班 ==")
import daemon  # noqa: E402

p = daemon.pending_slots(now=datetime(2026, 10, 30, 0, 30, 0),
                         check_times=["23:59"], state={"fired": {}}, catch_up_minutes=180)
check("T5.1 00:30 能补昨天 23:59（槽位键记昨天）",
      p == [("2026-10-29 23:59", 1860)], p)
p = daemon.pending_slots(now=datetime(2026, 10, 30, 0, 30, 0),
                         check_times=["23:59"],
                         state={"fired": {"2026-10-29 23:59": "x"}}, catch_up_minutes=180)
check("T5.2 已补过不重跑", p == [], p)
p = daemon.pending_slots(now=datetime(2026, 10, 30, 3, 30, 0),
                         check_times=["23:59"], state={"fired": {}}, catch_up_minutes=180)
check("T5.3 超出补发窗口（3:30 > 23:59+180min）不再补", p == [], p)
p = daemon.pending_slots(now=datetime(2026, 10, 30, 1, 0, 0),
                         check_times=["13:00", "23:59"], state={"fired": {}}, catch_up_minutes=180)
check("T5.4 凌晨 1 点只补 23:59 的班，不碰白天班次",
      [k for k, _ in p] == ["2026-10-29 23:59"], p)
p = daemon.pending_slots(now=datetime(2026, 10, 30, 13, 0, 10),
                         check_times=["13:00", "23:59"],
                         state={"fired": {"2026-10-29 23:59": "x"}}, catch_up_minutes=180)
check("T5.5 白天到点行为不变（今天 13:00 正常触发，昨班已记不重跑）",
      [k for k, _ in p] == ["2026-10-30 13:00"], p)
p = daemon.pending_slots(now=datetime(2026, 10, 30, 10, 0, 0),
                         check_times=["08:30"], state={"fired": {}}, catch_up_minutes=180)
check("T6.6 同日错过照旧补今天班（90 分钟 < 180 分钟窗口）",
      [k for k, _ in p] == ["2026-10-30 08:30"], p)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results), "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
