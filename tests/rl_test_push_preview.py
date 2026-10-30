# -*- coding: utf-8 -*-
"""推送预览单源守卫 + GET /api/push_preview 四态断言（临时目录隔离）。

收口目标：推送文案（title+body）只在 core/analyzer.py 维护一份——正常态
走 to_push_payload 同一条组装路径（页面所见 = 手机所得），前端
renderPushPreview 只渲染接口返回、绝不自拼。

G 组守卫：「游戏日常」不得出现在 ui/app.js 与 login_ui.py 源码里；
T 组功能：waiting_first_check（缺快照/坏快照）/ no_valid_records /
          正常态（逐字断言标题与正文行）/ 全完成态；
S 组副作用：预览全程只读快照——快照文件 mtime 不变、临时目录不新增文件
          （不联网、不推送、不写 push_history）。

隔离：读写两端都从 history.DATA_DIR 现场拼接路径（与生产写盘同一规则），
本测试重定向 history.DATA_DIR 即整体隔离，不会碰真实 data/。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import analyzer, history  # noqa: E402
import login_ui  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


# ---------- G 组：单源守卫 ----------
app_js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
login_src = (ROOT / "login_ui.py").read_text(encoding="utf-8")
analyzer_src = (ROOT / "core" / "analyzer.py").read_text(encoding="utf-8")
check("G1 ui/app.js 不再自拼「游戏日常」文案", "游戏日常" not in app_js)
check("G2 login_ui.py 无「游戏日常」文案（文案只在 analyzer）", "游戏日常" not in login_src)
check("G3 core/analyzer.py 持有推送文案（组装单源）", "游戏日常" in analyzer_src)

# ---------- 夹具：重定向 DATA_DIR，读写两端一起进临时盘 ----------
tmpdir = Path(tempfile.mkdtemp(prefix="rl-pushpreview-"))
history.DATA_DIR = tmpdir          # 写端（save_report_snapshot）与读端（push_preview_payload）
snap_path = tmpdir / history.REPORT_SNAPSHOT_NAME


def write_snap(payload):
    if payload is None:
        if snap_path.exists():
            snap_path.unlink()
        return
    snap_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def status(game_id, game_name, tasks, error=None, stale=None, no_account=None):
    """模拟 GameStatus.to_dict() 的序列化形态（finished 等派生字段重建时忽略）。"""
    return {"game_id": game_id, "game_name": game_name, "nickname": "",
            "source": "mock", "finished": None, "error": error, "stale": stale,
            "no_account": no_account, "done_count": 0, "total_count": 0,
            "tasks": tasks}


def task(name, done, progress=""):
    return {"name": name, "done": done, "progress": progress,
            "counted": True, "recover_seconds": None}


GENSHIN_UNFINISHED = status("genshin", "原神", [
    task("每日委托", True), task("消耗原粹树脂", False, "120/160"),
])
STARRAIL_DONE = status("starrail", "崩坏：星穹铁道", [task("清体力", True, "200/240")])


def preview():
    return login_ui.push_preview_payload()


# ---------- T 组：四态 ----------
# T1/T2 空态一：没跑过巡检（无快照）/ 快照损坏 → 等待第一次巡检
write_snap(None)
p = preview()
check("T1 无快照 → waiting_first_check", p["state"] == "waiting_first_check", p)
check("T1a 空态标题来自 analyzer 常量（文案单源）",
      p["title"] == analyzer.WAITING_FIRST_CHECK_TITLE, p.get("title"))

write_snap("这不是JSON{{{")
p = preview()
check("T2 快照损坏 → 仍 waiting_first_check（不 500）",
      p["state"] == "waiting_first_check" and p["ok"] is True, p)

# T3 空态二：快照在但 0 款有当日数据（全查询失败）→ 暂无有效记录
write_snap({"version": 1, "generated_at": "2026-10-31T08:00:00",
            "statuses": [
                status("genshin", "原神", [], error="登录凭证已失效"),
                status("starrail", "崩坏：星穹铁道", [], error="网络超时"),
            ],
            "resource_alerts": [], "activity_alerts": []})
p = preview()
check("T3 全失败 → no_valid_records",
      p["state"] == "no_valid_records" and p["title"] == analyzer.NO_VALID_RECORDS_TITLE, p)

# T4 正常态：1 完成 1 未完成 → 标题与正文逐字同源于 to_push_payload
write_snap({"version": 1, "generated_at": "2026-10-31T08:00:00",
            "statuses": [GENSHIN_UNFINISHED, STARRAIL_DONE],
            "resource_alerts": [], "activity_alerts": []})
before = snap_path.stat().st_mtime_ns
p = preview()
check("T4 正常态 state=ok", p["state"] == "ok" and p["ok"] is True, p)
check("T4a 标题与实发同式：1/2 已完成", p["title"] == "游戏日常：1/2 已完成", p.get("title"))
body = p.get("body") or ""
check("T4b 正文带快照时间戳", body.startswith("10-31 08:00"), body.splitlines()[:1])
check("T4c 未完成行含任务名与进度（实发粒度，非计数）",
      "[未完成] 原神：消耗原粹树脂(120/160)" in body, body)
check("T4d 已完成行逐字一致", "[已完成] 崩坏：星穹铁道" in body, body)
check("T4e 结尾行与实发一致", "共漏 1 项，别拖到 23:59。" in body, body)
check("T4f generated_at 回传", p.get("generated_at") == "2026-10-31T08:00:00", p.get("generated_at"))

# T5 全完成态
write_snap({"version": 1, "generated_at": "2026-10-31T20:30:00",
            "statuses": [
                status("genshin", "原神", [task("每日委托", True)]),
                STARRAIL_DONE,
            ],
            "resource_alerts": [], "activity_alerts": []})
p = preview()
check("T5 全完成标题：2/2 全部完成", p["title"] == "游戏日常：2/2 全部完成", p.get("title"))
check("T5a 全完成结尾行与实发一致",
      "今天的日常全部完成，可以放心摆烂了。" in (p.get("body") or ""), p.get("body"))

# ---------- S 组：只读副作用 ----------
before = snap_path.stat().st_mtime_ns   # 现场采样：上一组 write_snap 刚重写过文件
preview()
check("S1 预览只读快照（mtime 未变）", snap_path.stat().st_mtime_ns == before)
check("S2 临时目录无新增文件（不落 push_history/不写其他盘）",
      sorted(p.name for p in tmpdir.iterdir()) == [history.REPORT_SNAPSHOT_NAME],
      [p.name for p in tmpdir.iterdir()])

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
