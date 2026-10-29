# -*- coding: utf-8 -*-
"""data/push_history.json 结构化推送历史验证（临时目录隔离，不碰真实数据）。"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import notifier  # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="rl-pushhist-"))
notifier.PUSH_HISTORY_PATH = tmpdir / "push_history.json"

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

# T1 记录 + 读取 roundtrip（字段完整）
notifier.record_push_history("serverchan", True, "推送成功",
                             at=datetime(2026, 10, 29, 0, 48, 14))
items = notifier.load_push_history(8)
check("T1 记录字段完整（ts/channel/ok/message/title/body）",
      items == [{"ts": "2026-10-29 00:48:14", "channel": "serverchan",
                 "ok": True, "message": "推送成功", "title": "", "body": ""}], items)

# T2 上限裁剪 50 条
for i in range(55):
    notifier.record_push_history("qmsg", False, "fail %d" % i,
                                 at=datetime(2026, 10, 29, 1, 0, i % 60))
items = notifier.load_push_history(100)
check("T2 超出 50 条自动裁剪", len(items) == 50, len(items))
check("T3 最新在前（最后写入的排第一）", items[0]["message"] == "fail 54", items[0])

# T4/T5 push_all 汇聚点挂钩：dry_run 不记、真发记录
notifier.PUSH_HISTORY_PATH.write_text('{"version":1,"items":[]}', encoding="utf-8")
class FakeN:
    name = "serverchan"
    def send(self, title, body, dry_run=False):
        return SimpleNamespace(channel="serverchan", ok=True, message="推送成功", skipped=False)

notifier.push_all([FakeN()], "t", "b", dry_run=True)
check("T4 dry_run（不真实发送）不落历史", notifier.load_push_history(8) == [])
notifier.push_all([FakeN()], "游戏日常：3/5 已完成",
                  "原神：树脂已满 160/160\n星穹铁道：开拓力 200/240", dry_run=False)
items = notifier.load_push_history(8)
check("T5 真实发送落历史（成功 + 标题/正文入库，换行保留）",
      len(items) == 1 and items[0]["ok"] is True and items[0]["channel"] == "serverchan"
      and items[0]["title"] == "游戏日常：3/5 已完成"
      and items[0]["body"] == "原神：树脂已满 160/160\n星穹铁道：开拓力 200/240", items)

# T6 失败（PushError）也落历史
class BadN:
    name = "qmsg"
    def send(self, title, body, dry_run=False):
        raise notifier.PushError("缺少依赖 requests，请先执行：pip install -r requirements.txt")

notifier.push_all([BadN()], "游戏日常：2/5 已完成", "b", dry_run=False)
items = notifier.load_push_history(8)
check("T6 失败也落历史（ok=False + 错误消息 + 标题入库）",
      len(items) == 2 and items[0]["ok"] is False and "缺少依赖" in items[0]["message"]
      and items[0]["title"] == "游戏日常：2/5 已完成" and items[0]["body"] == "b", items)

# T7 文件损坏自动重建，不炸推送链路
notifier.PUSH_HISTORY_PATH.write_text("这不是 json", encoding="utf-8")
notifier.record_push_history("bark", True, "推送成功", at=datetime(2026, 10, 29, 2, 0, 0))
items = notifier.load_push_history(8)
check("T7 损坏文件自动重建", len(items) == 1 and items[0]["channel"] == "bark", items)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
