# -*- coding: utf-8 -*-
"""明日方舟「上次登录快照」陈旧判定（纯离线，零网络）。

背景（2026-10-30 实测）：
    森空岛 /api/v1/game/player/info 返回的是**上次登录时的快照**，
    不是实时数据。用户 10-29 22:14 之后没再进游戏，10-30 07:26 查询时
    routine.daily 仍原样返回 10/10（前一天的完成状态），理智也停在 25/205，
    于是软件把「今天没做」显示成了「今日已完成」。

覆盖：
  A1  快照早于今日 04:00 → 判陈旧，文案含"数据未更新"与上次在线时间
  A2  快照晚于今日 04:00 → 不判陈旧（正常查询路径）
  A3  凌晨 03:30（逻辑日仍属昨天）→ 昨天 22:14 的快照**不**算陈旧
  A4  日界边界 04:00 整 → 03:59 的快照算陈旧
  A5  lastOnlineTs 缺失 → 不判定（沿用旧行为，不误伤）
  A6  currentTs 缺失 → 不判定
  A7  端到端 fetch()：陈旧快照下 ok=True（不是查询失败）+ stale 非空、
      finished=False、unfinished_tasks 为空，说明文案可读，
      且 cred_kind 不是 "auth"（不会误报"凭证失效"）
  A7' 汇总层：stale 既不算未完成也不算失败，更不会让推送标题说「全部完成」
  A8  端到端 fetch()：新鲜快照下照常 10/10 且 finished=True（防误伤回归）
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import querier as Q  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

def epoch(dt):
    return int(dt.timestamp())

def payload(now_dt, online_dt, daily=(10, 10), ap=(25, 205)):
    """构造一份与真实返回同形状的 data（只保留被判定用到的字段）。"""
    node = {
        "currentTs": epoch(now_dt),
        "routine": {"daily": {"current": daily[0], "total": daily[1]},
                    "weekly": {"current": 12, "total": 13}},
        "status": {"ap": {"current": ap[0], "max": ap[1]}},
    }
    if online_dt is not None:
        node["status"]["lastOnlineTs"] = epoch(online_dt)
    return node

NOW = datetime(2026, 10, 30, 7, 26, 0)          # 真实复现时刻
LAST_NIGHT = datetime(2026, 10, 29, 22, 14, 0)  # 真实 lastOnlineTs

# ===================== A1 陈旧（真实复现场景） =====================
print("== A1 快照 10-29 22:14 / 现在 10-30 07:26 → 必须判陈旧 ==")
reason = Q._stale_snapshot_reason(payload(NOW, LAST_NIGHT))
check("A1a 判为陈旧", reason is not None, reason)
check("A1b 文案含「数据未更新」", bool(reason and "数据未更新" in reason), reason)
check("A1c 文案含上次在线时间", bool(reason and "10-29 22:14" in reason), reason)

# ===================== A2 新鲜 =====================
print("== A2 快照 10-30 05:00 → 必须不判陈旧 ==")
check("A2 不判陈旧", Q._stale_snapshot_reason(
    payload(NOW, datetime(2026, 10, 30, 5, 0))) is None)

# ===================== A3 凌晨（逻辑日仍是昨天） =====================
print("== A3 现在 10-30 03:30（逻辑日=10-29）/ 快照 10-29 22:14 → 不判陈旧 ==")
check("A3 不判陈旧", Q._stale_snapshot_reason(
    payload(datetime(2026, 10, 30, 3, 30), LAST_NIGHT)) is None)

# ===================== A4 日界边界 =====================
print("== A4 现在 10-30 04:00 整 / 快照 10-30 03:59 → 判陈旧 ==")
check("A4 判陈旧（刚过日界，快照属于昨天）", Q._stale_snapshot_reason(
    payload(datetime(2026, 10, 30, 4, 0), datetime(2026, 10, 30, 3, 59))) is not None)

# ===================== A5 / A6 字段缺失 =====================
print("== A5/A6 可选字段缺失 → 不判定 ==")
check("A5 lastOnlineTs 缺失不判", Q._stale_snapshot_reason(payload(NOW, None)) is None)
p_no_now = payload(NOW, LAST_NIGHT)
p_no_now.pop("currentTs")
check("A6 currentTs 缺失不判", Q._stale_snapshot_reason(p_no_now) is None)

# ===================== A7 / A8 端到端 fetch() =====================
print("== A7/A8 端到端 fetch()（monkeypatch，零网络） ==")

def make_account():
    return SimpleNamespace(game="arknights", game_name="明日方舟",
                           nickname="KiloDusk#0642", credential={"token": "t"})

def run_fetch(data):
    """把 SklandQuerier 的外部请求全部替换成固定返回，跑一次 fetch。"""
    orig = (Q.list_players, Q.get_arknights_player_info, Q.get_skland_credentials)
    Q.list_players = lambda cred, token: {"list": [{
        "appCode": "arknights",
        "bindingList": [{"uid": "11017xxxx", "nickName": "KiloDusk#0642",
                         "defaultRole": {"nickname": "KiloDusk#0642"}}],
    }]}
    Q.get_arknights_player_info = lambda cred, token, uid: data
    Q.get_skland_credentials = lambda cred, game, force_refresh=False: {
        "cred": "c", "token": "t"}
    try:
        return Q.SklandQuerier().fetch(make_account())
    finally:
        Q.list_players, Q.get_arknights_player_info, Q.get_skland_credentials = orig

stale = run_fetch(payload(NOW, LAST_NIGHT))
check("A7a 不再算查询失败（ok=True），改由 stale 标记", stale.ok is True, stale.error)
check("A7a2 stale 说明非空", bool(stale.stale), stale.stale)
check("A7b finished=False（不再冒充已完成）", stale.finished is False, stale.finished)
check("A7c unfinished_tasks 为空（不会误报未完成）",
      stale.unfinished_tasks == [], len(stale.unfinished_tasks))
check("A7d cred_kind 不是 auth（不触发凭证告警）",
      stale.cred_kind != "auth", stale.cred_kind)
check("A7e 说明文案可读", "数据未更新" in (stale.stale or ""), stale.stale)
check("A7f 序列化带 stale 字段（前端要据此画中性徽章）",
      "stale" in stale.to_dict() and bool(stale.to_dict()["stale"]),
      stale.to_dict().get("stale"))

# ============ A7' 汇总层：不催、不谎报全部完成、不算失败 ============
print("== A7' 汇总层（analyzer） ==")
from core.analyzer import DailyReport  # noqa: E402

only_stale = DailyReport(statuses=[stale])
check("A7g 不算未完成", only_stale.unfinished == [], len(only_stale.unfinished))
check("A7h 不算查询失败", only_stale.failed == [], len(only_stale.failed))
check("A7i 不算已完成", only_stale.finished == [], len(only_stale.finished))
check("A7j 不触发催促推送", only_stale.need_alert is False, only_stale.need_alert)
_title, _body = only_stale.to_push_payload()
check("A7k 推送标题不说「全部完成」", "全部完成" not in _title, _title)
check("A7l 推送正文点名待更新，别当成已完成",
      "数据待更新" in _body and "明日方舟" in _body, _body.replace("\n", " / "))
check("A7m 控制台汇总区分待更新", "数据待更新" in only_stale.to_console_text())

# 混合：另一款已完成的真数据 + 方舟待更新 → 标题不能是「2/2 全部完成」
from core.models import GameStatus, TaskItem  # noqa: E402

other_done = GameStatus(
    game_id="endfield", game_name="终末地", source="skland",
    tasks=[TaskItem("每日活跃度", True, "100/100")],
)
mixed = DailyReport(statuses=[other_done, stale])
check("A7n 混合场景标题不是全完成",
      "全部完成" not in mixed.to_push_payload()[0], mixed.to_push_payload()[0])
check("A7o 混合场景不把方舟算进未完成",
      [s.game_name for s in mixed.unfinished] == [], [s.game_name for s in mixed.unfinished])

fresh = run_fetch(payload(NOW, datetime(2026, 10, 30, 5, 0)))
check("A8a 新鲜快照 ok=True", fresh.ok is True, fresh.error)
check("A8b 新鲜快照 finished=True", fresh.finished is True, fresh.finished)
check("A8c 进度仍是 10/10",
      [t.progress for t in fresh.tasks if t.name == "每日任务"] == ["10/10"],
      [t.progress for t in fresh.tasks])

# ===================== 汇总 =====================
passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print("\n=== rl_test_ark_stale.py: %d PASS / %d FAIL ===" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
