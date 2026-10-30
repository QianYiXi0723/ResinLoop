"""批次二并发收口的回归测试。

覆盖四个修复点：
  T1 daemon.run_check 重入门闩——调度/托盘并发只跑一轮
  T2 skland cred 缓存并发不丢条目（锁内重读 + mkstemp 原子写）
  T3 activities notified 合并以磁盘为准（refresh 不再回滚守护进程的去重标记）
  T4 login_ui live_status 并发 force 刷新只打一次接口

运行：python tests/rl_test_concurrency_guard.py
纪律：涉及磁盘的路径常量全部重定向到 %TEMP%；网络调用（run_once /
collect_statuses / _exchange_cred）全部打桩——全程零真实请求。
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="rl-concguard-"))
PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + (("（%s）" % detail) if detail else ""))


# ---------- T1 daemon.run_check 重入门闩 ----------
import daemon
import main

_calls = {"run_once": 0}


def _fake_run_once(cfg, dry_run=False):
    _calls["run_once"] += 1
    time.sleep(0.5)
    return 0


def _fake_apply(cfg):
    pass


main.run_once = _fake_run_once
main._apply_mihoyo_query_config = _fake_apply

codes = []


def _t1_worker():
    codes.append(daemon.run_check("T1"))


t1 = threading.Thread(target=_t1_worker)
t1.start()
time.sleep(0.15)          # 让 t1 进入打桩的 run_once（持门闩中）
t2 = threading.Thread(target=_t1_worker)
t2.start()
t1.join()
t2.join()
check("T1 run_check 重入被跳过（只跑一轮）",
      _calls["run_once"] == 1 and len(codes) == 2 and all(c == 0 for c in codes),
      "run_once 调用 %d 次" % _calls["run_once"])


# ---------- T2 skland cred 缓存并发不丢条目 ----------
import core.skland_api as skland

skland._CRED_CACHE_DIR = TMP / "skland"


def _fake_exchange(tok):
    time.sleep(0.05)      # 拉长网络窗口，放大旧代码「整份覆盖丢条目」的概率
    return {"cred": "cred_" + tok, "token": tok}


skland._exchange_cred = _fake_exchange

errs = []


def _t2_worker(tok):
    try:
        skland.get_skland_credentials({"token": tok}, game="arknights")
    except Exception as exc:  # noqa: BLE001
        errs.append(str(exc))


ths = []
for i in range(6):
    th = threading.Thread(target=_t2_worker, args=("tok%d" % i,))
    th.start()
    time.sleep(0.03)      # 错峰启动：让后启动者在别人落盘前完成 load（旧代码丢条目场景）
    ths.append(th)
[t.join() for t in ths]
cred_file = TMP / "skland" / "skland_cred_arknights.json"
data = json.loads(cred_file.read_text(encoding="utf-8"))
check("T2 skland cred 缓存并发不丢条目",
      len(data) == 6 and not errs,
      "keys=%d errs=%d" % (len(data), len(errs)))


# ---------- T3 activities notified 合并以磁盘为准 ----------
import core.activities as acts

acts.CACHE_PATH = TMP / "activities.json"
acts.CACHE_PATH.write_text(json.dumps({
    "version": acts.CACHE_VERSION,
    "items": [{"game": "genshin", "kind": "act", "title": "旧活动", "remote_id": "r1"}],
    "notified": {"2026-10-31": ["genshin:act:r1"]},   # daemon 刚 mark 的（磁盘最新）
    "fetched_at": "2026-10-30 01:00:00",
}, ensure_ascii=False), encoding="utf-8")

stale = acts.load_cache()          # refresh 数秒前 load 的旧快照
stale["notified"] = {}             # 旧快照里没有 daemon 的标记
stale["items"] = [{"game": "genshin", "kind": "act", "title": "新活动", "remote_id": "r2"}]
stale["fetched_at"] = "2026-10-31 04:00:00"
acts._merge_notified_and_save(stale)

after = json.loads(acts.CACHE_PATH.read_text(encoding="utf-8"))
check("T3 notified 合并以磁盘为准不被回滚",
      after["notified"].get("2026-10-31") == ["genshin:act:r1"]
      and after["items"][0]["remote_id"] == "r2",
      "notified=%s" % after["notified"])


# ---------- T4 live_status 并发 force 刷新只打一次接口 ----------
import login_ui

login_ui._LIVE_CACHE["at"] = 0.0                       # 进程缓存置为过期
login_ui._LIVE_DISK_CACHE = TMP / "live_cache.json"    # 磁盘缓存也指到临时目录
collect_calls = {"n": 0}


def _fake_collect(cfg):
    collect_calls["n"] += 1
    time.sleep(0.5)
    return []


main.collect_statuses = _fake_collect

live_results = []


def _t4_worker():
    live_results.append(login_ui.live_status(force=True))


t1 = threading.Thread(target=_t4_worker)
t1.start()
time.sleep(0.15)
t2 = threading.Thread(target=_t4_worker)
t2.start()
t1.join()
t2.join()
check("T4 live 并发刷新只打一次接口",
      collect_calls["n"] == 1 and len(live_results) == 2
      and all(r.get("ok") for r in live_results),
      "collect %d 次" % collect_calls["n"])


shutil.rmtree(TMP, ignore_errors=True)
print("=== rl_test_concurrency_guard.py: %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)))
sys.exit(0 if not FAIL else 1)
