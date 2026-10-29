# -*- coding: utf-8 -*-
"""推送标题口径回归（纯内存，不联网、不写盘）。

背景：2026-10-29 收到「游戏日常：1/5 个未完成」，正文却是「已完成 2 款」+
「查询失败 2 款」，读起来像数字对不上。根因是标题分子统计的是**未完成**
游戏数，而不是完成数。现统一改为「已完成/参与检查数」。

覆盖：
  T1 2 完成 + 1 未完成 + 2 失败  -> 2/5 已完成
  T2 全部完成                    -> 5/5 全部完成
  T3 0 完成 + 3 未完成 + 2 失败  -> 0/5 已完成
  T4 失败不参与「全部完成」判定    -> 有失败时绝不出现「全部完成」
  T5 体力预警后缀仍拼在标题末尾

运行：python tests/rl_test_push_title.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.analyzer import DailyReport          # noqa: E402
from core.models import GameStatus, TaskItem   # noqa: E402

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


def task(name: str, done: bool) -> TaskItem:
    return TaskItem(name=name, done=done, progress="1/1" if done else "0/1")


def status(game_id: str, game_name: str, done: bool) -> GameStatus:
    return GameStatus(
        game_id=game_id,
        game_name=game_name,
        nickname="测试",
        tasks=[task("每日活跃度", done)],
        source="test",
    )


def failed(game_id: str, game_name: str) -> GameStatus:
    return GameStatus(
        game_id=game_id,
        game_name=game_name,
        nickname="测试",
        tasks=[],
        source="test",
        error="查询异常：retcode=1034",
    )


# T1 本次真实场景：2 完成 / 1 未完成 / 2 失败
report = DailyReport(statuses=[
    status("starrail", "崩坏：星穹铁道", True),
    status("arknights", "明日方舟", True),
    status("endfield", "终末地", False),
    failed("genshin", "原神"),
    failed("zzz", "绝区零"),
])
title, body = report.to_push_payload()
check("T1 标题 = 2/5 已完成", title == "游戏日常：2/5 已完成", title)
check("T1 正文仍列出失败", "[查询失败] 原神、绝区零" in body, body)

# T2 全部完成
title_all, _ = DailyReport(statuses=[
    status("a", "A", True), status("b", "B", True),
]).to_push_payload()
check("T2 标题 = 2/2 全部完成", title_all == "游戏日常：2/2 全部完成", title_all)

# T3 一个都没做完 + 两个失败
title_zero, _ = DailyReport(statuses=[
    status("a", "A", False), status("b", "B", False), status("c", "C", False),
    failed("d", "D"), failed("e", "E"),
]).to_push_payload()
check("T3 标题 = 0/5 已完成", title_zero == "游戏日常：0/5 已完成", title_zero)

# T4 有查询失败时不能宣称全部完成
report_fail = DailyReport(statuses=[status("a", "A", True), failed("b", "B")])
title_fail, _ = report_fail.to_push_payload()
check("T4 有失败不出现「全部完成」", "全部完成" not in title_fail, title_fail)
check("T4 标题 = 1/2 已完成", title_fail == "游戏日常：1/2 已完成", title_fail)

# T5 体力预警后缀
from core.models import ResourceAlert  # noqa: E402

report_alert = DailyReport(
    statuses=[status("a", "A", True), status("b", "B", False)],
    resource_alerts=[ResourceAlert(
        game_id="b", game_name="B", nickname="测试", resource="理智",
        current=349, maximum=350, threshold=300,
    )],
)
title_alert, _ = report_alert.to_push_payload()
check(
    "T5 标题带体力后缀",
    title_alert == "游戏日常：1/2 已完成 · ⚠️ 体力将溢出（1）",
    title_alert,
)

print(f"\n=== 推送标题口径回归: {PASS} PASS / {FAIL} FAIL ===")
sys.exit(0 if FAIL == 0 else 1)
