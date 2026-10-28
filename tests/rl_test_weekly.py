# -*- coding: utf-8 -*-
"""功能②③ 后端单元测试（临时目录，绝不碰真实 data/ 与 logs/）。

覆盖：
  T1 history.weekly_digest       7 天混合数据的逐游戏统计
  T2 daemon.format_weekly_body   周报文案组装（含全勤/漏做/空数据变体）
  T3 daemon.weekly_slot_week / weekly_due  周报调度边界（周日 21:00/周一补发/不重发）
  T4 querier 恢复字段            parse_genshin / parse_starrail / parse_endfield
                                 + normalize_recover_seconds 的时间戳统一化
运行：python rl_test_weekly.py（在项目根目录）
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # tests/ 的上一级 = 项目根
sys.path.insert(0, str(BASE))

import core.history as history          # noqa: E402
import daemon                            # noqa: E402
from core.querier import (               # noqa: E402
    MiyousheQuerier,
    SklandQuerier,
    normalize_recover_seconds,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}  {detail}")


# ---- 重定向 history 存档到临时目录（gamedate 测试验证过的模式） ----
TMP = Path(tempfile.mkdtemp(prefix="rl_weekly_"))
history.DATA_DIR = TMP
history.HISTORY_PATH = TMP / "history.json"

# ---------------------------------------------------------------- T1 统计
keys = ["2026-10-23", "2026-10-24", "2026-10-25",
        "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
# 每款游戏 7 天的序列：k=查到数据 / n=当天没查到；第二位=是否完成
plan = {
    "genshin":   [("k", 1)] * 7,                          # 7/7 全勤
    "starrail":  [("k", 1)] * 5 + [("k", 0), ("n", 0)],   # 查 6 天完 5 天 → 漏 1
    "zzz":       [("k", 0)] * 4 + [("n", 0)] * 3,         # 查 4 天全没完 → 漏 4
    "endfield":  [("n", 0)] * 4 + [("k", 1)] * 3,         # 查 3 天全完
    # arknights 整周无记录 → 不进结果
}
days = {}
for i, key in enumerate(keys):
    day = {}
    for game, seq in plan.items():
        kind, fin = seq[i]
        day[game] = {"known": kind == "k", "finished": bool(fin),
                     "unfinished_tasks": 0 if fin else 2,
                     "last": "2026-10-29 13:00:00"}
    days[key] = day
history._atomic_write({"version": 1, "days": days})

digest = history.weekly_digest(7)
check("T1a 天数=7", digest["days_count"] == 7, str(digest["days_count"]))
by = {g["game"]: g for g in digest["games"]}
check("T1b 原神 7/7 漏 0",
      by["genshin"]["done_days"] == 7 and by["genshin"]["missed_days"] == 0, str(by.get("genshin")))
check("T1c 星铁 查6完5漏1",
      by["starrail"]["known_days"] == 6 and by["starrail"]["done_days"] == 5
      and by["starrail"]["missed_days"] == 1, str(by.get("starrail")))
check("T1d 绝区零 漏 4", by["zzz"]["missed_days"] == 4, str(by.get("zzz")))
check("T1e 终末地 完 3", by["endfield"]["done_days"] == 3, str(by.get("endfield")))
check("T1f 方舟整周无记录不出现", "arknights" not in by, str(sorted(by)))
check("T1g 游戏中文名来自 KNOWN_GAMES",
      by["genshin"]["name"] == "原神" and by["endfield"]["name"] == "终末地",
      str(by["genshin"]["name"]))

# ---------------------------------------------------------------- T2 文案
body = daemon.format_weekly_body(digest)
check("T2a 全勤条目", "原神 7 天全勤" in body, body)
check("T2b 漏做条目", "崩坏：星穹铁道 完成 5 天、漏做 1 次" in body, body)
check("T2c 漏做条目2", "绝区零 完成 0 天、漏做 4 次" in body, body)
check("T2d 未漏不标漏", "终末地 完成 3 天。" in body, body)
check("T2e 开头结尾", body.startswith("本周：") and body.endswith("有漏做，下周继续加油！"), body)

body_all = daemon.format_weekly_body({
    "days_count": 7,
    "games": [{"game": "genshin", "name": "原神", "done_days": 7,
               "known_days": 7, "missed_days": 0}],
})
check("T2f 全勤结尾", body_all.endswith("全部完成，可以安心摆烂了。"), body_all)
check("T2g 空数据返回空串",
      daemon.format_weekly_body({"days_count": 0, "games": []}) == "")
check("T2h 整周没查到的游戏不进文案",
      daemon.format_weekly_body({
          "days_count": 7,
          "games": [{"game": "zzz", "name": "绝区零", "done_days": 0,
                     "known_days": 0, "missed_days": 0}],
      }) == "")

# ---------------------------------------------------------------- T3 调度
sun = datetime(2026, 11, 1, 21, 0)        # 周日 21:00（2026-11-01 实际是周日）
sun_before = datetime(2026, 11, 1, 20, 59)
prev_sun = datetime(2026, 10, 25, 21, 0)  # 上周日 21:00
mon = datetime(2026, 11, 2, 9, 0)         # 周一早上
sat = datetime(2026, 11, 7, 12, 0)        # 下周六（同一 ISO 周）
check("T3a 测试基准 11-01 是周日", datetime(2026, 11, 1).weekday() == 6)

w_this = daemon.weekly_slot_week(sun)
w_prev = daemon.weekly_slot_week(sun_before)
check("T3b 周日 21:00 前指向上周槽位", w_prev == daemon.weekly_slot_week(prev_sun),
      f"{w_prev} vs {daemon.weekly_slot_week(prev_sun)}")
check("T3c 21:00 起切本周", w_this != w_prev, f"{w_this} vs {w_prev}")
check("T3d 周六仍是本周槽位", daemon.weekly_slot_week(sat) == w_this)

check("T3e 到点未发→发", daemon.weekly_due(sun, {"week": w_prev}))
check("T3f 已发→不重发", not daemon.weekly_due(sun, {"week": w_this}))
check("T3g 周一早上补发", daemon.weekly_due(mon, {"week": w_prev}))
check("T3h 周内一直补发直到发出", daemon.weekly_due(sat, {"week": w_prev}))
check("T3i 补发后不再发", not daemon.weekly_due(sat, {"week": w_this}))
check("T3j 周日 20:59 上周已发→不发", not daemon.weekly_due(sun_before, {"week": w_prev}))
check("T3k 状态丢失→会再发一次", daemon.weekly_due(sun, {}))
import re as _re
check("T3l 槽位键是 ISO 周格式", bool(_re.match(r"^\d{4}-W\d{2}$", w_this)), w_this)

# ---------------------------------------------------------------- T4 恢复字段
q = MiyousheQuerier()
tasks = q.parse_genshin({
    "finished_task_num": 4, "total_task_num": 4,
    "current_resin": 150, "max_resin": 160, "resin_recovery_time": 4900,
    "current_expedition_num": 1, "max_expedition_num": 5,
    "expeditions": [{"status": "Ongoing"}],
})
resin = next(t for t in tasks if t.name == "原粹树脂")
check("T4a 原神透传 resin_recovery_time", resin.recover_seconds == 4900,
      str(resin.recover_seconds))

tasks2 = q.parse_starrail({
    "current_train_score": 500, "max_train_score": 500,
    "current_stamina": 238, "max_stamina": 240, "stamina_recover_time": 720,
    "rogue_tourn_weekly_cur": 3, "rogue_tourn_weekly_max": 3,
})
check("T4b 星铁透传 stamina_recover_time",
      next(t for t in tasks2 if t.name == "开拓力").recover_seconds == 720)

tasks3 = q.parse_starrail({
    "current_train_score": 1, "max_train_score": 500,
    "current_stamina": 100, "max_stamina": 240,
    "rogue_tourn_weekly_cur": 0, "rogue_tourn_weekly_max": 3,
})
check("T4c 无恢复字段→None",
      next(t for t in tasks3 if t.name == "开拓力").recover_seconds is None)

now = time.time()
detail = {"dailyMission": {"dailyActivation": 100, "maxDailyActivation": 120},
          "dungeon": {"curStamina": "91", "maxStamina": "350",
                      "maxTs": str(int(now) + 3600)}}   # 实测字符串数字，照抄
ef = next(t for t in SklandQuerier.parse_endfield(detail) if t.name == "理智")
check("T4d 终末地 maxTs(秒级,字符串)→距满秒数",
      ef.recover_seconds is not None and 3598 <= ef.recover_seconds <= 3600,
      str(ef.recover_seconds))
detail_ms = {"dailyMission": {"dailyActivation": 100, "maxDailyActivation": 120},
             "dungeon": {"curStamina": "91", "maxStamina": "350",
                         "maxTs": int((now + 3600) * 1000)}}   # 毫秒变体
ef2 = next(t for t in SklandQuerier.parse_endfield(detail_ms) if t.name == "理智")
check("T4e 终末地 maxTs(毫秒)→同一结果",
      ef2.recover_seconds is not None and 3598 <= ef2.recover_seconds <= 3600,
      str(ef2.recover_seconds))

check("T4f None 原样", normalize_recover_seconds(None) is None)
check("T4g 剩余秒数直通", normalize_recover_seconds(4900) == 4900)
check("T4h 秒时间戳", 3598 <= (normalize_recover_seconds(int(now) + 3600) or 0) <= 3600)
check("T4i 毫秒时间戳", 3598 <= (normalize_recover_seconds(int((now + 3600) * 1000)) or 0) <= 3600)
check("T4j 过去的时间戳→0", normalize_recover_seconds(int(now) - 100) == 0)
check("T4k 非数字→None", normalize_recover_seconds("abc") is None)

# TaskItem.to_dict 带出新字段
check("T4l to_dict 带出 recover_seconds",
      resin.to_dict().get("recover_seconds") == 4900, str(resin.to_dict()))

print(f"\n=== rl_test_weekly.py: {PASS} PASS / {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
