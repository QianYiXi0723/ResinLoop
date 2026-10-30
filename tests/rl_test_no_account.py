# -*- coding: utf-8 -*-
"""「账号下没这款游戏」不等于查询失败（纯离线，零网络）。

背景（2026-10-30 用户补充）：
    勾选了某款游戏并扫码，但这个米游社/森空岛账号**从来没玩过它**。
    之前的行为：米游社 credential 里 uid 还是模板占位 100000000 →
    MiyousheAuthError「缺少 uid」；森空岛 binding 找不到 → SklandQueryError
    「当前森空岛账号没有绑定 X」—— 两边都画红字「查询失败」，还可能触发
    凭证失效告警，但凭证其实是好的。

覆盖：
  B1  extract_credentials：uid 缺失 / 占位 100000000 → MiyousheNotBoundError
  B2  米游社端到端：NotBound 异常 → GameStatus(no_account=...)，ok=True
  B3  森空岛端到端：binding 找不到 → GameNotBoundError → no_account，ok=True
  B4  汇总层：no_account 不算未完成/失败/已完成、不触发催促、标题不说「全部完成」
  B5  推送正文点名「未创建角色」；控制台有 SKIP 标记与汇总
  B6  序列化：GameStatus.to_dict 带 no_account 字段（前端要据此画中性徽章）
  B7  历史记录：no_account 不置 known（报表不画成"已知、漏 0 项"）
  B8  正常账号不受影响（防误伤回归）
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import querier as Q  # noqa: E402
from core.models import GameStatus, TaskItem  # noqa: E402
from core.analyzer import DailyReport  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

# ===================== B1 米游社 uid 判定 =====================
print("== B1 extract_credentials：uid 缺失 / 模板占位 → NotBound ==")
from core.mihoyo_api import MiyousheNotBoundError, PLACEHOLDER_UID  # noqa: E402

check("B1a 占位常量 = 模板值 100000000", PLACEHOLDER_UID == "100000000", PLACEHOLDER_UID)


def account_with(game, credential):
    return SimpleNamespace(game=game, game_name={
        "genshin": "原神", "zzz": "绝区零"}[game], credential=credential)


def extract_of(cred, game="genshin"):
    from core.mihoyo_api import extract_credentials as ex
    return ex(account_with(game, cred))


for tag, uid_value in (("缺失", None), ("占位", "100000000")):
    cred = {"ltuid": "1", "ltoken": "x"}
    if uid_value:
        cred["uid"] = uid_value
    try:
        extract_of(cred)
        check(f"B1 uid {tag} → NotBound", False, "没有抛异常")
    except MiyousheNotBoundError as exc:
        check(f"B1 uid {tag} → NotBound", True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check(f"B1 uid {tag} → NotBound", False, f"抛了别的异常：{exc}")

# 真实 uid 不受影响
try:
    extract_of({"ltuid": "1", "ltoken": "x", "uid": "900123456"})
    check("B1 真实 uid 正常通过", True)
except Exception as exc:  # noqa: BLE001
    check("B1 真实 uid 正常通过", False, exc)

# ===================== B2 米游社端到端 =====================
print("== B2 米游社 fetch()：NotBound → no_account（monkeypatch，零网络） ==")

def run_mihoyo_fetch(cred, game="genshin"):
    """只替换外部依赖：extract_credentials 返回占位 UID，daily_note 返回固定数据。
    两个都必须 patch —— 漏一个就会打真实接口（零网络铁律）。"""
    orig = (Q.extract_credentials, Q.request_daily_note)
    Q.extract_credentials = lambda account: ("1", "x", "100000000", "cn_gf01", "c=1")
    Q.request_daily_note = lambda **kw: {"finished_task_num": 4, "total_task_num": 4}
    try:
        acc = SimpleNamespace(game=game, game_name="原神", nickname="n", credential=cred)
        return Q.MiyousheQuerier().fetch(acc)
    finally:
        Q.extract_credentials, Q.request_daily_note = orig


def run_mihoyo_notbound(cred, game="genshin"):
    """extract_credentials 抛 NotBound（占位 UID 场景的真实路径）。"""
    orig = (Q.extract_credentials, Q.request_daily_note)
    Q.extract_credentials = lambda account: (_ for _ in ()).throw(
        MiyousheNotBoundError("账号下还没有《原神》的角色 UID"))
    Q.request_daily_note = lambda **kw: (_ for _ in ()).throw(AssertionError("不应发请求"))
    try:
        acc = SimpleNamespace(game=game, game_name="原神", nickname="n", credential=cred)
        return Q.MiyousheQuerier().fetch(acc)
    finally:
        Q.extract_credentials, Q.request_daily_note = orig

status = run_mihoyo_notbound({"uid": "100000000"})
check("B2a ok=True（不是查询失败）", status.ok is True, status.error)
check("B2b no_account 非空", bool(status.no_account), status.no_account)
check("B2c finished=False", status.finished is False, status.finished)
check("B2d cred_kind 不是 auth（不触发凭证告警）", status.cred_kind != "auth", status.cred_kind)

# ===================== B3 森空岛端到端 =====================
print("== B3 森空岛 fetch()：没绑这款游戏 → no_account ==")

def run_skland_fetch(players):
    orig = (Q.list_players, Q.get_skland_credentials)
    Q.list_players = lambda cred, token: players
    Q.get_skland_credentials = lambda cred, game, force_refresh=False: {
        "cred": "c", "token": "t"}
    try:
        acc = SimpleNamespace(game="endfield", game_name="终末地", nickname="n",
                              credential={"token": "t"})
        return Q.SklandQuerier().fetch(acc)
    finally:
        Q.list_players, Q.get_skland_credentials = orig

# 只绑了方舟、没绑终末地的森空岛账号
players_no_endfield = {"list": [{
    "appCode": "arknights",
    "bindingList": [{"uid": "1", "nickName": "n"}],
}]}
sk = run_skland_fetch(players_no_endfield)
check("B3a ok=True（不是查询失败）", sk.ok is True, sk.error)
check("B3b no_account 非空且点名游戏", "终末地" in (sk.no_account or ""), sk.no_account)
check("B3c finished=False", sk.finished is False, sk.finished)

# bindingList 为空的极端形态
sk_empty = run_skland_fetch({"list": [{
    "appCode": "endfield", "bindingList": []}]})
check("B3d bindingList 空 → no_account", sk_empty.ok is True and bool(sk_empty.no_account),
      sk_empty.no_account)

# ===================== B4/B5 汇总层 =====================
print("== B4/B5 汇总层（analyzer） ==")
report = DailyReport(statuses=[sk])
check("B4a 不算未完成", report.unfinished == [], len(report.unfinished))
check("B4b 不算查询失败", report.failed == [], len(report.failed))
check("B4c 不算已完成", report.finished == [], len(report.finished))
check("B4d 不触发催促推送", report.need_alert is False, report.need_alert)
title, body = report.to_push_payload()
check("B4e 推送标题不说「全部完成」", "全部完成" not in title, title)
check("B5a 推送正文点名未创建角色", "未创建角色" in body and "终末地" in body,
      body.replace("\n", " / "))
check("B5e 纯建号场景不出现「共漏 0 项」", "共漏 0 项" not in body,
      body.replace("\n", " / "))
check("B5f 纯建号场景结尾给行动指引", "建号后自动生效" in body, body.replace("\n", " / "))
console = report.to_console_text()
check("B5b 控制台有 SKIP 标记", "SKIP" in console)
check("B5c 控制台汇总含未创建角色计数", "未创建角色 1" in console)
check("B5d summary 序列化带 no_account",
      report.to_dict()["summary"].get("no_account") == 1)

# 混合：正常完成 + 未创建角色 → 标题不能「2/2 全部完成」
other_done = GameStatus(game_id="arknights", game_name="明日方舟", source="skland",
                        tasks=[TaskItem("每日任务", True, "10/10")])
mixed = DailyReport(statuses=[other_done, sk])
check("B4f 混合场景标题不是全完成",
      "全部完成" not in mixed.to_push_payload()[0], mixed.to_push_payload()[0])

# ===================== B6 序列化 =====================
check("B6 to_dict 带 no_account 字段",
      "no_account" in sk.to_dict() and bool(sk.to_dict()["no_account"]),
      sk.to_dict().get("no_account"))

# ===================== B7 历史记录 =====================
print("== B7 history.record：no_account 不置 known ==")
import core.history as H  # noqa: E402

tmp_dir = Path(tempfile.mkdtemp(prefix="rltest_noacc_"))
orig_path, orig_data_dir = H.HISTORY_PATH, H.DATA_DIR
H.DATA_DIR = tmp_dir                      # tmp 文件也写到临时盘，避免跨盘 os.replace
H.HISTORY_PATH = tmp_dir / "history.json"
try:
    H.record(DailyReport(statuses=[sk],
                         generated_at=datetime(2026, 10, 30, 22, 0, 0)))
    data = H.load()
    node = data["days"]["2026-10-30"]["endfield"]
    check("B7a known=False（报表不画成已知）", node.get("known") is False, node)
    check("B7b checks/last 仍然更新", node.get("checks") == 1 and node.get("last") == "22:00",
          node)
    # 真数据的游戏同一轮写入 → known=True
    H.record(DailyReport(statuses=[other_done],
                         generated_at=datetime(2026, 10, 30, 22, 0, 0)))
    data = H.load()
    node2 = data["days"]["2026-10-30"]["arknights"]
    check("B7c 正常游戏 known=True 不受影响", node2.get("known") is True, node2)
finally:
    H.HISTORY_PATH, H.DATA_DIR = orig_path, orig_data_dir

# ===================== 汇总 =====================
passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print("\n=== rl_test_no_account.py: %d PASS / %d FAIL ===" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
