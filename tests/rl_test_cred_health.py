# -*- coding: utf-8 -*-
"""凭证健康记账单元测试（账本路径 monkeypatch 到 %TEMP%，绝不写仓库 data\）。

覆盖（任务书 S2）：
  ① 连续 2 次 kind="auth" 失败 → 第 2 次返回 alert；第 3 次 24h 内不再返回
  ② alerted_at 在 24h 内 → 不返回；跨过 24h 后可再返回
  ③ 成功后 streak 归零、kind 清空，再次失败从 1 重新计
  ④ kind="other" 连续失败 5 次 → 永不返回 alert
  ⑤ 写入后 json.loads 可解析，两次 record 之间 items 不被整包覆盖
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.cred_health as ch  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="rl_credh_"))
ch.CRED_HEALTH_PATH = TMP / "cred_health.json"

NOW = datetime(2026, 10, 30, 12, 0, 0)
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


# ---------------------------------------------------------------- T1 首次告警
r1 = ch.record("1001:genshin", False, kind="auth", now=NOW)
check("T1a 第 1 次失败不告警", r1 is None, str(r1))
r2 = ch.record("1001:genshin", False, kind="auth", now=NOW + timedelta(minutes=10))
check("T1b 第 2 次返回 alert（streak=2, kind=auth）",
      r2 is not None and r2["streak"] == 2 and r2["kind"] == "auth", str(r2))
check("T1c game_id 取冒号后一段", r2 is not None and r2["game_id"] == "genshin", str(r2))
r3 = ch.record("1001:genshin", False, kind="auth", now=NOW + timedelta(minutes=20))
check("T1d 24h 内第 3 次不再返回", r3 is None, str(r3))

# ---------------------------------------------------------------- T2 冷却期
r4 = ch.record("1001:genshin", False, kind="auth", now=NOW + timedelta(hours=25))
check("T2 跨过 24h 后可再告警（streak=4）", r4 is not None and r4["streak"] == 4, str(r4))

# ---------------------------------------------------------------- T3 成功清零
rn = ch.record("1001:genshin", True, now=NOW + timedelta(hours=25, minutes=5))
check("T3a 成功返回 None", rn is None, str(rn))
data = json.loads(ch.CRED_HEALTH_PATH.read_text(encoding="utf-8"))
node = data["items"]["1001:genshin"]
check("T3b streak 归零且 kind 清空", node["fail_streak"] == 0 and node["kind"] == "", str(node))
rf = ch.record("1001:genshin", False, kind="auth", now=NOW + timedelta(hours=25, minutes=10))
check("T3c 再失败从 1 重新计（不告警）", rf is None, str(rf))

# ---------------------------------------------------------------- T4 other 永不告警
ra = None
for i in range(5):
    ra = ch.record("2002:zzz", False, kind="other", now=NOW + timedelta(minutes=i))
check("T4a kind=other 连续 5 次永不告警", ra is None, str(ra))
data = json.loads(ch.CRED_HEALTH_PATH.read_text(encoding="utf-8"))
zzz = data["items"]["2002:zzz"]
check("T4b other streak 正常累计（=5）", zzz["fail_streak"] == 5 and zzz["kind"] == "other", str(zzz))

# ---------------------------------------------------------------- T5 持久化
check("T5a 文件可 json.loads 且两个 key 并存",
      set(data["items"]) == {"1001:genshin", "2002:zzz"}, str(sorted(data["items"])))
check("T5b version 字段在", data.get("version") == 1, str(data.get("version")))

# ---------------------------------------------------------------- T6 reset_all
ch.reset_all()
data = json.loads(ch.CRED_HEALTH_PATH.read_text(encoding="utf-8"))
check("T6 reset_all 清空账本", data["items"] == {}, str(data))

print(f"\n=== rl_test_cred_health.py: {PASS} PASS / {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
