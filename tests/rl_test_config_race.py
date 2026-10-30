# -*- coding: utf-8 -*-
"""save_raw_config 写入竞态收口验证（临时目录隔离）。

收口前的 bug：每个保存函数都是「读整份 config.json → 改一个字段 → 整份写回」，
不带锁并发时后写者拿着旧快照整份覆盖，先写者的字段被静默丢掉（无报错无日志）。

收口方案：9 个 save_raw_config 调用方全部挂 @_serialized_write（非重入
_CONFIG_WRITE_LOCK），同一进程内的「读→改→写」临界区串行化。

本测试两路并发各改不同字段各 50 轮，断言两字段终值都在——任何一方被
旧快照覆盖即 FAIL。另附结构断言：8 个模块级保存函数 + 1 个 handler 方法
必须都带装饰器（防未来新增调用方裸奔）。

注意：全程不起 HTTP、不碰真实 config.json（CONFIG_PATH 连同其 parent
一起重定向到临时盘，save_raw_config 的 mkstemp 临时文件落在同盘 parent，
不会触发跨盘 os.replace 的 WinError 17）。
"""
import inspect
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DEFAULT_CONFIG  # noqa: E402
import login_ui  # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="rl-configrace-"))
cfg = tmpdir / "config.json"
login_ui.CONFIG_PATH = cfg   # 路径常量重定向：读、写、临时文件、滚动备份全部落在临时盘

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


ROUNDS = 50
THRESHOLD_START = 100            # genshin 阈值第 i 轮 = 100+i（终值 100+ROUNDS-1）
SCHEDULE_LAST = ["08:49", "20:30"]  # 时间点线程最后一轮写入值（"08:%02d" % 49 + "20:30"）

# 夹具：出厂默认配置（保证过 parse_config 全量复检）
base = json.loads(json.dumps(DEFAULT_CONFIG))
cfg.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

errors = []   # (worker, round, exc)；线程内任何异常都算失败，不许静默吞掉


def threshold_worker():
    for i in range(ROUNDS):
        try:
            login_ui.save_thresholds({
                "genshin": THRESHOLD_START + i, "starrail": 240, "zzz": 200,
                "arknights": 135, "endfield": 300,
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(("thresholds", i, exc))
            return


def schedule_worker():
    for i in range(ROUNDS):
        try:
            login_ui.save_schedule(["08:%02d" % (i % 60), "20:30"])
        except Exception as exc:  # noqa: BLE001
            errors.append(("schedule", i, exc))
            return


t1 = threading.Thread(target=threshold_worker)
t2 = threading.Thread(target=schedule_worker)
t1.start()
t2.start()
t1.join()
t2.join()

check("T0 两线程 50 轮全部无异常", not errors, errors[:1])

final = json.loads(cfg.read_text(encoding="utf-8"))
check("T1 体力阈值终值在（未被旧快照覆盖）",
      final.get("app", {}).get("genshin_resin_threshold") == THRESHOLD_START + ROUNDS - 1,
      final.get("app", {}).get("genshin_resin_threshold"))
check("T2 时间点终值在（未被旧快照覆盖）",
      final.get("app", {}).get("check_times") == SCHEDULE_LAST,
      final.get("app", {}).get("check_times"))

# 结构断言：所有 save_raw_config 调用方必须持锁（防未来新增裸奔调用方）
MODULE_FNS = (
    "set_account_enabled", "save_thresholds", "save_query_proxy",
    "save_catch_up", "save_activity_alert", "save_schedule",
    "write_mihoyo", "write_skland",
)
for name in MODULE_FNS:
    src = inspect.getsource(getattr(login_ui, name))
    check("S1 %s 已挂 @_serialized_write" % name,
          src.lstrip().startswith("@_serialized_write"))

handle_src = inspect.getsource(login_ui.Handler._handle_save_push)
check("S1 Handler._handle_save_push 已挂 @_serialized_write",
      handle_src.lstrip().startswith("@_serialized_write"))

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
