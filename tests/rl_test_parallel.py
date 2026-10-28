# -*- coding: utf-8 -*-
"""极速查询重构验证：顺序保持 / 失败隔离 / 并行提速 / 设备指纹稳定。"""
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from core.models import GameStatus, TaskItem  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

GAMES = ["genshin", "starrail", "zzz", "arknights", "endfield"]

def make_config(games, provider="mock"):
    accounts = [
        SimpleNamespace(game=g, game_name=g, nickname="n-" + g, credential={},
                        enabled=True)
        for g in games
    ]
    return SimpleNamespace(
        enabled_accounts=accounts,
        disabled_accounts=[],
        query=SimpleNamespace(provider=provider, mihoyo_enabled=True),
        test_mode="auto",
    )

class FakeQuerier:
    """可编程假查询器：delay 秒眠，raise_at 命中时抛异常。"""
    def __init__(self, delay=0.0, raise_at=None):
        self.delay = delay
        self.raise_at = raise_at
        self.source = "fake"

    def fetch(self, account):
        time.sleep(self.delay)
        if self.raise_at == account.game:
            raise RuntimeError("模拟网络爆炸")
        return GameStatus(
            game_id=account.game, game_name=account.game,
            nickname=account.nickname,
            tasks=[TaskItem(name="每日委托", done=True)],
        )

# ================= T1 顺序保持 =================
print("== T1 数据结构与顺序和原版完全一致 ==")
fake = FakeQuerier()
main.get_querier = lambda game, provider="mock": fake
cfg = make_config(GAMES)
statuses = main.collect_statuses(cfg)
check("T1.1 返回条数 = 账号数", len(statuses) == 5, len(statuses))
check("T1.2 顺序与 accounts 严格一致",
      [s.game_id for s in statuses] == GAMES,
      [s.game_id for s in statuses])
check("T1.3 结构字段原样（ok/finished/game_name/nickname）",
      all(s.ok and s.finished and s.nickname == "n-" + s.game_id for s in statuses))

# ================= T2 失败隔离 =================
print("== T2 单账号失败兜底 ==")
class BoomQuerier(FakeQuerier):
    def fetch(self, account):
        if account.game == "zzz":
            raise RuntimeError("模拟网络爆炸")
        return super().fetch(account)
main.get_querier = lambda game, provider="mock": BoomQuerier()
statuses = main.collect_statuses(make_config(GAMES))
by_id = {s.game_id: s for s in statuses}
check("T2.1 顺序仍严格一致", [s.game_id for s in statuses] == GAMES)
check("T2.2 爆炸的游戏变成 error 态（ok=False，错误消息在）",
      by_id["zzz"].ok is False and "模拟网络爆炸" in (by_id["zzz"].error or ""),
      by_id["zzz"].error)
check("T2.3 其它四款完全不受影响",
      all(by_id[g].ok and by_id[g].finished for g in GAMES if g != "zzz"))

# ================= T3 并行提速 =================
print("== T3 并行提速证明（5 个各睡 1 秒的查询）==")
main.get_querier = lambda game, provider="mock": FakeQuerier(delay=1.0)
t0 = time.monotonic()
statuses = main.collect_statuses(make_config(GAMES))
elapsed = time.monotonic() - t0
check("T3.1 五个 1 秒查询并行跑（远小于串行 5 秒）",
      len(statuses) == 5 and elapsed < 3.0, "elapsed=%.2fs" % elapsed)
check("T3.2 全部真实执行（≥1 秒）且未退化成串行",
      1.0 <= elapsed < 3.0, "elapsed=%.2fs" % elapsed)

# ================= T4 设备指纹并行稳定（mihoyo_api） =================
print("== T4 设备指纹并行稳定（防风控回归）==")
from core import mihoyo_api  # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="rl-parallel-"))
mihoyo_api._FP_CACHE_PATH = tmpdir / "device_fp.json"
mihoyo_api._PROXY = None

def get_fp(game):
    return mihoyo_api._get_device_info(game)[1]

with ThreadPoolExecutor(max_workers=3) as pool:
    fp_cold = dict(zip(["genshin", "starrail", "zzz"],
                       list(pool.map(get_fp, ["genshin", "starrail", "zzz"]))))
with ThreadPoolExecutor(max_workers=3) as pool:
    fp_warm = dict(zip(["genshin", "starrail", "zzz"],
                       list(pool.map(get_fp, ["genshin", "starrail", "zzz"]))))
check("T4.1 冷缓存并行：三款各自拿到非空指纹",
      all(fp_cold[g] for g in fp_cold), {g: v[:8] for g, v in fp_cold.items()})
check("T4.2 指纹零抖动（第二轮与第一轮完全相同——串行版同语义）",
      fp_cold == fp_warm)
check("T4.3 缓存文件为合法 JSON 且含 3 个游戏条目",
      len(json.loads(mihoyo_api._FP_CACHE_PATH.read_text(encoding="utf-8"))) == 3)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
