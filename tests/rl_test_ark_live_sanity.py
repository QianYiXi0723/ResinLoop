# -*- coding: utf-8 -*-
"""明日方舟理智实时推算验证（纯离线，零网络）。

背景（2026-10-31 受控观测）：
    森空岛 player/info 是登录快照，`status.ap.current` 冻结在快照时刻
    （实测 9/205，真实约 49）；但 `status.ap` 还带 `completeRecoveryTime`
    （回满时刻）与 `lastApAddTime`，理智恢复是确定性的 6 分钟/点——
    任意时刻的真实理智都能算出来。官方森空岛 App 显示的就是推算值，
    两次逐分钟对账吻合（47@05:34、48@05:39:27，含"15小时40分钟后
    全部恢复"的相对时长反推）。

覆盖：
  B1  现算：真实复现值锁定（05:47:47 → 49/205，recover_seconds=55900）
  B2  已回满（回满时刻过去）→ max、recover_seconds=0、done=True
  B3  无 completeRecoveryTime → 回退快照值（旧行为，recover_seconds=None）
  B4  毫秒级时间戳 → 正确换算
  B5  防御：推算为负 → 0 下限；current/crt 全缺 → None
  B6  fresh 端到端 parse_arknights：理智为推算值且带 recover_seconds
  B7  stale 端到端 fetch：横幅文案带推算理智与回满 ETA，
      tasks 附带推算理智（counted=False），不冒充完成/未完成
  B8  stale 且无恢复字段（旧数据形态）→ 横幅维持原文案、零附带任务
  B9  汇总层：stale+推算理智不进未完成/失败/已完成、不触发催促
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import querier as Q          # noqa: E402
from core.analyzer import DailyReport  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


def epoch(dt):
    return int(dt.timestamp())


def payload(now_dt, online_dt, ap_cur=9, ap_max=205, crt=None, last_add=None,
            daily=(0, 10)):
    """与真实返回同形状的 data；crt/last_add 缺省不带（旧数据形态）。"""
    node = {
        "currentTs": epoch(now_dt),
        "routine": {"daily": {"current": daily[0], "total": daily[1]},
                    "weekly": {"current": 12, "total": 13}},
        "status": {"ap": {"current": ap_cur, "max": ap_max}},
    }
    if online_dt is not None:
        node["status"]["lastOnlineTs"] = epoch(online_dt)
    if crt is not None:
        node["status"]["ap"]["completeRecoveryTime"] = crt
    if last_add is not None:
        node["status"]["ap"]["lastApAddTime"] = last_add
    return node


# 真实复现值（2026-10-31 探针）：05:47:47 采样、回满时刻 21:19:27、快照冻结 9/205
NOW_TS = 1793396867            # 10-31 05:47:47
CRT = 1793452767               # 10-31 21:19:27
NOW = datetime.fromtimestamp(NOW_TS)
LOGIN = datetime.fromtimestamp(1793395010)   # 10-31 05:16:50

# ===================== B1 现算（真实复现值锁定） =====================
print("== B1 现算：05:47:47 → 49/205（与官方口径对账） ==")
task = Q._arknights_sanity_task(payload(NOW, LOGIN, crt=CRT, last_add=NOW_TS - 14600))
check("B1a progress=49/205", task is not None and task.progress == "49/205",
      task.progress if task else None)
check("B1b recover_seconds=55900（回满剩余秒）",
      task is not None and task.recover_seconds == CRT - NOW_TS,
      task.recover_seconds if task else None)
check("B1c 未满（done=False）", task is not None and task.done is False)
check("B1d 仅展示不参与判定（counted=False）",
      task is not None and task.counted is False)

# ===================== B2 已回满 =====================
print("== B2 回满时刻过去 → max / 0 / done ==")
task = Q._arknights_sanity_task(payload(NOW, LOGIN, ap_cur=205, crt=NOW_TS - 100))
check("B2a progress=205/205", task is not None and task.progress == "205/205",
      task.progress if task else None)
check("B2b recover_seconds=0", task is not None and task.recover_seconds == 0)
check("B2c done=True", task is not None and task.done is True)

# ===================== B3 无恢复字段 → 回退快照值 =====================
print("== B3 无 completeRecoveryTime → 快照值 9/205、无 recover_seconds ==")
task = Q._arknights_sanity_task(payload(NOW, LOGIN))
check("B3a 回退快照值 9/205", task is not None and task.progress == "9/205",
      task.progress if task else None)
check("B3b recover_seconds=None（调用方可区分推算/快照）",
      task is not None and task.recover_seconds is None)

# ===================== B4 毫秒级时间戳 =====================
print("== B4 completeRecoveryTime 毫秒级 → 正确换算 ==")
task = Q._arknights_sanity_task(payload(NOW, LOGIN, crt=CRT * 1000))
check("B4 progress=49/205（与秒级一致）",
      task is not None and task.progress == "49/205",
      task.progress if task else None)

# ===================== B5 防御 =====================
print("== B5 防御：异常值不炸、不越界 ==")
task = Q._arknights_sanity_task(payload(NOW, LOGIN, ap_cur=9, crt=NOW_TS + 400 * 360 * 3))
check("B5a 推算为负 → 0 下限", task is not None and task.progress == "0/205",
      task.progress if task else None)
check("B5b current 与 crt 全缺 → None",
      Q._arknights_sanity_task({
          "currentTs": NOW_TS, "status": {"ap": {"max": 205}}}) is None)

# ===================== B6 fresh 端到端 parse =====================
print("== B6 parse_arknights（fresh）：理智为推算值 ==")
fresh_data = payload(NOW, NOW, ap_cur=9, crt=CRT, daily=(0, 10))
tasks = Q.SklandQuerier.parse_arknights(fresh_data)
check("B6a 每日任务 0/10 在且参与判定",
      tasks and tasks[0].name == "每日任务" and tasks[0].progress == "0/10"
      and tasks[0].counted is True)
check("B6b 理智为推算值 49/205 且带 recover_seconds",
      len(tasks) > 1 and tasks[1].progress == "49/205"
      and tasks[1].recover_seconds == CRT - NOW_TS,
      tasks[1].progress if len(tasks) > 1 else None)


# ===================== B7 stale 端到端 fetch（真实复现 05:34 形态） =====================
print("== B7 stale fetch：横幅带推算理智 47/205，tasks 附带推算值 ==")

# 真实探针形态（2026-10-31 05:34:11）：登录 05:16 已跨日同步，但查询对象是
# 01:47 的冻结快照（daily 仍 10/10），crt=今晚 21:19 在未来 → stale + 可推算
NOW31_0534 = datetime.fromtimestamp(1793396051)   # 10-31 05:34:11
ONLINE0147 = datetime.fromtimestamp(1793382467)   # 10-31 01:47:47


def make_account():
    return SimpleNamespace(game="arknights", game_name="明日方舟",
                           nickname="KiloDusk#0642", credential={"token": "t"})


def run_fetch(data):
    """替换 SklandQuerier 的外部请求为固定返回（monkeypatch，零网络）。"""
    orig = (Q.list_players, Q.get_arknights_player_info, Q.get_skland_credentials)
    Q.list_players = lambda cred, token: {"list": [{
        "appCode": "arknights",
        "bindingList": [{"uid": "110172522", "nickName": "KiloDusk#0642",
                         "defaultRole": {"nickname": "KiloDusk#0642"}}],
    }]}
    Q.get_arknights_player_info = lambda cred, token, uid: data
    Q.get_skland_credentials = lambda cred, game, force_refresh=False: {
        "cred": "c", "token": "t"}
    try:
        return Q.SklandQuerier().fetch(make_account())
    finally:
        Q.list_players, Q.get_arknights_player_info, Q.get_skland_credentials = orig


stale = run_fetch(payload(NOW31_0534, ONLINE0147, ap_cur=9, crt=CRT,
                          last_add=1793382207, daily=(10, 10)))
check("B7a stale 非空且含「数据未更新」与上次在线",
      bool(stale.stale and "数据未更新" in stale.stale
           and "01:47" in stale.stale), stale.stale)
check("B7b 横幅带推算理智与回满 ETA（47/205、预计 21:19）",
      "按恢复推算" in (stale.stale or "") and "47/205" in (stale.stale or "")
      and "21:19" in (stale.stale or ""), stale.stale)
check("B7c tasks 附带推算理智（counted=False）",
      len(stale.tasks) == 1 and stale.tasks[0].progress == "47/205"
      and stale.tasks[0].counted is False
      and stale.tasks[0].recover_seconds == 56716,
      [t.progress for t in stale.tasks])
check("B7d 不冒充完成/未完成", stale.finished is False
      and stale.unfinished_tasks == [], stale.unfinished_tasks)
check("B7e 仍是 ok=True + cred_kind 非 auth", stale.ok is True
      and stale.cred_kind != "auth", stale.cred_kind)

# ===================== B8 stale 无恢复字段（旧形态兼容） =====================
print("== B8 stale 且无 crt：横幅维持原文案、零附带任务 ==")
stale_old = run_fetch(payload(NOW31_0534, ONLINE0147, daily=(10, 10)))
check("B8a 横幅不含推算字样（原文案不变）",
      bool(stale_old.stale) and "按恢复推算" not in stale_old.stale
      and stale_old.stale.endswith("无法确认今日进度"), stale_old.stale)
check("B8b 零附带任务（旧行为）", stale_old.tasks == [], stale_old.tasks)

# ===================== B9 汇总层 =====================
print("== B9 汇总层：stale+推算理智不改变判定 ==")
report = DailyReport(statuses=[stale])
check("B9a 不算未完成", report.unfinished == [], len(report.unfinished))
check("B9b 不算查询失败", report.failed == [], len(report.failed))
check("B9c 不算已完成", report.finished == [], len(report.finished))
check("B9d 不触发催促推送", report.need_alert is False, report.need_alert)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
