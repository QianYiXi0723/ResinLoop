# -*- coding: utf-8 -*-
"""config.json 滚动备份（.bak-1/.bak-2/.bak-3）验证（临时目录隔离）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from login_ui import (  # noqa: E402
    CONFIG_BACKUP_KEEP,
    _backup_config_before_write,
    save_raw_config,
)

tmpdir = Path(tempfile.mkdtemp(prefix="rl-configbak-"))
cfg = tmpdir / "config.json"

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))

def bak(i):
    p = cfg.parent / f"config.json.bak-{i}"
    return p.read_text(encoding="utf-8") if p.exists() else None

def write_cfg(text):
    cfg.write_text(text, encoding="utf-8")

# 重定向路径到临时目录
import login_ui  # noqa: E402
login_ui.CONFIG_PATH = cfg

# T1 config.json 不存在时不炸、不产出备份
_backup_config_before_write()
check("T1 config.json 缺失时静默跳过", not any(cfg.parent.glob("config.json.bak-*")))

# T2 首次备份：bak-1 = 当前内容
write_cfg('{"v": 1}')
_backup_config_before_write()
check("T2 首次备份 bak-1 内容一致", bak(1) == '{"v": 1}' and bak(2) is None)

# T3 第二次：bak-1 换新，旧内容滚到 bak-2
write_cfg('{"v": 2}')
_backup_config_before_write()
check("T3 轮转：bak-1=新 / bak-2=旧",
      bak(1) == '{"v": 2}' and bak(2) == '{"v": 1}')

# T4 第三次：三条齐
write_cfg('{"v": 3}')
_backup_config_before_write()
check("T4 三份齐备", bak(1) == '{"v": 3}' and bak(2) == '{"v": 2}' and bak(3) == '{"v": 1}')

# T5 第四次：超出上限，最老的被顶掉，永不出现 bak-4
write_cfg('{"v": 4}')
_backup_config_before_write()
baks = sorted(p.name for p in cfg.parent.glob("config.json.bak-*"))
check("T5 上限裁剪（只保留 %d 份，无 bak-4）" % CONFIG_BACKUP_KEEP,
      bak(1) == '{"v": 4}' and bak(2) == '{"v": 3}' and bak(3) == '{"v": 2}'
      and baks == ["config.json.bak-1", "config.json.bak-2", "config.json.bak-3"], baks)

# T6 集成：save_raw_config 真实写入前自动备份
data = json.loads(cfg.read_text(encoding="utf-8"))
data["new_key"] = "added"
save_raw_config(data)
check("T6 save_raw_config 写入成功", json.loads(cfg.read_text(encoding="utf-8"))["new_key"] == "added")
check("T7 写入前的旧内容进了 bak-1", bak(1) == '{"v": 4}')

# T8 备份内容可解析（不是半截文件）
ok = all(isinstance(json.loads(bak(i)), dict) for i in (1, 2, 3))
check("T8 三份备份均为合法 JSON", ok)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)
