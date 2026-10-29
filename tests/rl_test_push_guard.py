# -*- coding: utf-8 -*-
"""推送回压单元测试（账本路径 monkeypatch 到 %TEMP%，绝不写仓库 data\）。

覆盖（任务书 S3-S5、S7）：
  ① 连续 5 次失败 → 第 5 次返回「已静默 6 小时」，第 6 次 is_muted=True
  ② muted_until 过期 → is_muted=False 且文件里已清空写回
  ③ 任一次成功 → streak=0 且 muted_until 清空
  ④ 写盘可 json.loads；不同 channel 互不覆盖
  S4 无静默时 _active_notifiers 与 build_notifiers 一致、push_all 恰好 1 次
  S5 全通道静默 → push_all 零调用；部分静默 → 被静默通道不在 active 里
  S7 同 game 双账号 → 记账 key 不同、连击互不污染
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.push_guard as pg  # noqa: E402
import main  # noqa: E402
from core.config import AccountConfig, AppConfig, TEST_MODE_FORCE_ALERT  # noqa: E402
from core.models import PushResult  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="rl_pushg_"))
pg.PUSH_FAIL_PATH = TMP / "push_fail.json"

NOW = None  # record/is_muted 内部用真实时钟（muted_until=now+6h 落在未来，无需 mock）
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


# ---------------------------------------------------------------- S3① 连续失败静默
msg = None
for i in range(5):
    msg = pg.record("serverchan", False)
check("S3①a 第 5 次失败返回「已静默 6 小时」", msg == "已静默 6 小时", str(msg))
check("S3①b 第 5 次后 is_muted=True", pg.is_muted("serverchan") is True)
msg6 = pg.record("serverchan", False)
check("S3①c 静默期内再失败仍返回静默提示", msg6 == "已静默 6 小时", str(msg6))
check("S3①d is_muted 仍 True", pg.is_muted("serverchan") is True)

# ---------------------------------------------------------------- S3④ 持久化与隔离
data = json.loads(pg.PUSH_FAIL_PATH.read_text(encoding="utf-8"))
check("S3④a 文件可 json.loads 且 version 在", data.get("version") == 1)
pg.record("qmsg", False)
data = json.loads(pg.PUSH_FAIL_PATH.read_text(encoding="utf-8"))
check("S3④b 不同 channel 互不覆盖",
      set(data["items"]) == {"serverchan", "qmsg"}
      and data["items"]["serverchan"]["streak"] == 6
      and data["items"]["qmsg"]["streak"] == 1, str(data["items"]))

# ---------------------------------------------------------------- S3② 到期自动清
data = json.loads(pg.PUSH_FAIL_PATH.read_text(encoding="utf-8"))
data["items"]["serverchan"]["muted_until"] = "2026-01-01 00:00:00"  # 早已过期
pg.PUSH_FAIL_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
check("S3②a 过期后 is_muted=False", pg.is_muted("serverchan") is False)
data = json.loads(pg.PUSH_FAIL_PATH.read_text(encoding="utf-8"))
check("S3②b 过期清空已写回文件", data["items"]["serverchan"]["muted_until"] is None,
      str(data["items"]["serverchan"]))

# ---------------------------------------------------------------- S3③ 成功全清
pg.reset_all()
pg.record("serverchan", False)  # streak=1（<5，不静默）
check("S3③a 失败一次后未静默", pg.is_muted("serverchan") is False)
msg = pg.record("serverchan", True)
check("S3③b 成功返回 None", msg is None, str(msg))
data = json.loads(pg.PUSH_FAIL_PATH.read_text(encoding="utf-8"))
node = data["items"]["serverchan"]
check("S3③c streak=0 且 muted_until 清空", node["streak"] == 0 and node["muted_until"] is None, str(node))

# ---------------------------------------------------------------- S4/S5/S7 run_once 级
import core.history as history  # noqa: E402
import core.cred_health as ch  # noqa: E402

history.DATA_DIR = TMP                       # 必须连 DATA_DIR 一起 patch：
history.HISTORY_PATH = TMP / "history.json"  # _atomic_write 的 mkstemp 用 DATA_DIR，跨盘会 WinError 17
ch.CRED_HEALTH_PATH = TMP / "cred_health.json"
pg.PUSH_FAIL_PATH = TMP / "push_fail.json"

CALLS = []


class FakeNotifier:
    def __init__(self, name):
        self.name = name

    def send(self, title, body):
        return PushResult(channel=self.name, ok=True, message="测试")


def fake_build(push_cfg):
    return [FakeNotifier("serverchan"), FakeNotifier("qmsg")]


def fake_push_all(notifiers, title, body, dry_run=False):
    CALLS.append((title, [n.name for n in notifiers], dry_run))
    return [PushResult(channel=n.name, ok=True, message="测试") for n in notifiers]


class FakeQuerier:
    source = "fake"

    def __init__(self, fail_for):
        self.fail_for = fail_for

    def fetch(self, account):
        if account.nickname == self.fail_for:
            from core.models import GameStatus
            return GameStatus(game_id=account.game, game_name=account.game_name,
                              nickname=account.nickname, error="模拟失败",
                              cred_kind="other")
        from core.querier import get_querier  # 借真 mock 的成功路径太重，直接构造成功态
        from core.models import TaskItem, GameStatus
        return GameStatus(game_id=account.game, game_name=account.game_name,
                          nickname=account.nickname,
                          tasks=[TaskItem("每日委托", True, "4/4")])


def make_config():
    return AppConfig(
        accounts=[AccountConfig(game="genshin", nickname="博士"),
                  AccountConfig(game="genshin", nickname="管理员")],
        activity_alert_enabled=False,
        test_mode=TEST_MODE_FORCE_ALERT,
    )


# S4 无静默：active 与 build_notifiers 一致；push_all 恰好 1 次
main.build_notifiers = fake_build
main.push_all = fake_push_all
main.get_querier = lambda game, provider=None: FakeQuerier(None)
pg.reset_all()
cfg = make_config()
code = main.run_once(cfg, dry_run=False)
check("S4a run_once 退出码 0", code == 0, str(code))
check("S4b push_all 恰好被调用 1 次", len(CALLS) == 1, str(CALLS))
check("S4c 两个通道都在发送名单里", CALLS and sorted(CALLS[0][1]) == ["qmsg", "serverchan"], str(CALLS))

# S5 静默生效：两个通道全部打到静默 → run_once 里 push_all 零调用
pg.reset_all()
for _ in range(5):
    pg.record("serverchan", False)
    pg.record("qmsg", False)
CALLS.clear()
code = main.run_once(cfg, dry_run=False)
check("S5a 全静默时 push_all 零调用", len(CALLS) == 0, str(CALLS))
active = main._active_notifiers(cfg.push)
check("S5b 被静默通道不在 active 列表", active == [], str([n.name for n in active]))

# S5c 部分静默：只静默 serverchan → active 只剩 qmsg
pg.reset_all()
for _ in range(5):
    pg.record("serverchan", False)
active = main._active_notifiers(cfg.push)
check("S5c 部分静默只滤掉被静默通道", [n.name for n in active] == ["qmsg"], str([n.name for n in active]))

# S7 同 game 双账号：key 不同、连击互不污染
pg.reset_all()
main.get_querier = lambda game, provider=None: FakeQuerier("博士")  # 博士失败，管理员成功
main.run_once(cfg, dry_run=False)
data = json.loads(ch.CRED_HEALTH_PATH.read_text(encoding="utf-8"))
keys = sorted(k for k in data["items"] if k.endswith(":genshin"))
check("S7a 同 game 双账号记账 key 不同", len(keys) == 2 and "博士:genshin" in keys
      and "管理员:genshin" in keys, str(keys))
bo, ad = data["items"]["博士:genshin"], data["items"]["管理员:genshin"]
check("S7b 失败者 streak=1", bo["fail_streak"] == 1 and bo["kind"] == "other", str(bo))
check("S7c 成功者 streak=0 不被污染", ad["fail_streak"] == 0, str(ad))

print(f"\n=== rl_test_push_guard.py: {PASS} PASS / {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
