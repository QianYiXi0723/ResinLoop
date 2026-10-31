# -*- coding: utf-8 -*-
"""实时体力自动刷新（refresh=auto）年龄判定离线验证（临时目录零网络）。

背景（2026-10-31 用户反馈「刷新加载得太慢」）：
    「每次打开控制台都强刷」实测让 F5 也要真查 ~14 秒。修法：首刷改发
    refresh=auto，服务端按磁盘缓存 generated_at 的年龄决定真查还是秒开
    （阈值 _LIVE_AUTO_REFRESH_MINUTES=10，服务端时钟判定）。

覆盖（直接驱动 do_GET 内同一套判定逻辑抽出的判定函数）：
  E1  缓存缺失 → 强制真查
  E2  generated_at 解析失败/缺失 → 强制真查
  E3  缓存 2 分钟前（<10 分钟阈值）→ 不真查（秒开）
  E4  缓存 20 分钟前（>阈值）→ 强制真查
  E5  阈值边界：恰好 10 分钟 → 不真查（> 才查）
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import login_ui  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


tmpdir = Path(tempfile.mkdtemp(prefix="rl-liveauto-"))
log_dir = tmpdir / "logs"
log_dir.mkdir()
# 坑（老规矩）：_LIVE_DISK_CACHE 是模块加载期从 BASE_DIR 派生的常量，
# 只 patch BASE_DIR 不跟变——派生常量必须一并 patch
login_ui.BASE_DIR = tmpdir
login_ui._LIVE_DISK_CACHE = tmpdir / "data" / "live_cache.json"


def should_force(generated_at, now=None):
    """与 /api/live 路由里的 auto 判定逐字同逻辑（抽出来测）。"""
    disk = None
    if generated_at is not None:
        payload = {"generated_at": generated_at, "games": [{"game": "x"}]}
        (tmpdir / "data").mkdir(exist_ok=True)
        import json
        (tmpdir / "data" / "live_cache.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        disk = login_ui._live_disk_read()
    gen_dt = None
    if disk is not None:
        try:
            gen_dt = datetime.strptime(str(disk.get("generated_at") or ""),
                                       "%Y-%m-%d %H:%M:%S")
        except ValueError:
            gen_dt = None
    return (disk is None or gen_dt is None or
            ((now or datetime.now()) - gen_dt).total_seconds()
            > login_ui._LIVE_AUTO_REFRESH_MINUTES * 60)


now = datetime.now().replace(microsecond=0)   # 清微秒：字符串往返后边界差值才是整数秒
check("E1 缓存缺失 → 真查", should_force(None) is True)
check("E2 generated_at 坏值 → 真查", should_force("not-a-date") is True)
check("E3 缓存 2 分钟前 → 秒开",
      should_force((now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
                   now=now) is False)
check("E4 缓存 20 分钟前 → 真查",
      should_force((now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),
                   now=now) is True)
check("E5 恰好 10 分钟 → 秒开（严格大于才查）",
      should_force((now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
                   now=now) is False)
check("E6 阈值常量=10 分钟", login_ui._LIVE_AUTO_REFRESH_MINUTES == 10)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
