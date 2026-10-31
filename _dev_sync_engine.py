# -*- coding: utf-8 -*-
"""把当前工作树同步进 engine/，用于「不打包 zip 也能测 exe 形态」。

背景
----
ResinLoop.exe 只是 C# 壳，真正的业务代码全在 engine/ 里（engine/python 便携
解释器 + engine/*.py + engine/ui）。所以改代码不需要重新编译 exe，只要把改动
同步进 engine/，再启动 ResinLoop.exe 即可。

打包脚本 _build_release.py 会从工作树重新组装一份 release/*/engine；本脚本做
的是同一件事，但落在本地 engine/ 上，并且刻意不碰：

  * engine/python      便携解释器（几万个文件，搬一次很慢）
  * engine/config.json 本地测试配置（含你的真实账号，绝不覆盖）
  * engine/logs、engine/data  运行期数据

用法
----
    python _dev_sync_engine.py           同步
    python _dev_sync_engine.py --check   只检查差异，不写盘（有漂移退出码 1）
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
PYTHONW = ENGINE / "python" / "pythonw.exe"
ENTRY_TEMPLATE = ROOT / "shell" / "engine_app.py"

#: (相对路径, 是否需要 daemon 去托盘改写)
PY_FILES = ("login_ui.py", "main.py", "daemon.py")
DAEMON_NEEDLE = "return run_daemon(use_tray=not args.no_tray)"
DAEMON_PATCHED = (
    "# exe 发行版：托盘由 C# 壳负责（ResinLoop.exe --tray），daemon 恒为 headless\n"
    "    return run_daemon(use_tray=False)"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def daemon_bytes() -> bytes:
    """工作树 daemon.py + 去托盘改写（与发行包完全一致的处理）。"""
    src = (ROOT / "daemon.py").read_text(encoding="utf-8")
    if src.count(DAEMON_NEEDLE) != 1:
        raise SystemExit(
            f"[FATAL] daemon.py 托盘开关锚点异常（出现 {src.count(DAEMON_NEEDLE)} 次）"
        )
    return src.replace(DAEMON_NEEDLE, DAEMON_PATCHED).encode("utf-8")


def plan() -> list[tuple[Path, bytes, str]]:
    """返回 [(目标文件, 新内容, 说明)]，顺序即写入顺序。"""
    items: list[tuple[Path, bytes, str]] = []

    for name in PY_FILES:
        src = ROOT / name
        body = daemon_bytes() if name == "daemon.py" else src.read_bytes()
        items.append((ENGINE / name, body, "源码"))

    for src in sorted((ROOT / "core").glob("*.py")):
        items.append((ENGINE / "core" / src.name, src.read_bytes(), "core"))

    for name in ("index.html", "style.css", "app.js"):
        items.append((ENGINE / "ui" / name, (ROOT / "ui" / name).read_bytes(), "前端"))

    items.append((ENGINE / "app.py", ENTRY_TEMPLATE.read_bytes(), "引擎入口"))

    for src in sorted((ROOT / "tools").glob("*")):
        if src.is_file() and not src.name.startswith("."):
            items.append((ENGINE / "tools" / src.name, src.read_bytes(), "tools"))

    for src in sorted((ROOT / "static" / "icons").rglob("*")):
        if src.is_file():
            rel = src.relative_to(ROOT / "static" / "icons")
            items.append((ENGINE / "static" / "icons" / rel, src.read_bytes(), "图标"))

    return items


def main() -> int:
    check_only = "--check" in sys.argv[1:]

    if not PYTHONW.exists():
        print(f"[FATAL] 缺少便携解释器：{PYTHONW}")
        print("        engine/python 是发行包的核心，先跑一次 _build_release.py")
        print("        或从 release/ResinLoop_v2.0/engine 整目录拷过来。")
        return 2
    if not ENTRY_TEMPLATE.exists():
        print(f"[FATAL] 缺少引擎入口模板：{ENTRY_TEMPLATE}")
        return 2

    changes: list[tuple[Path, bytes, str]] = []
    for target, body, group in plan():
        old = target.read_bytes() if target.exists() else None
        if old is None or digest(old) != digest(body):
            changes.append((target, body, group))

    if check_only:
        # 同步只增不删：工作树里删掉/改名的文件会在 engine/ 永久残留——
        # --check 顺带把这类残留报出来（只报不删，清理由人决定）
        expected = {target for target, _body, _group in plan()}
        extras: list[Path] = []
        covered = [ENGINE / name for name in PY_FILES] + [
            ENGINE / "app.py", ENGINE / "core", ENGINE / "ui",
            ENGINE / "tools", ENGINE / "static" / "icons",
        ]
        for root in covered:
            if root.is_file():
                if root not in expected:
                    extras.append(root)
            elif root.is_dir():
                extras.extend(
                    f for f in root.rglob("*")
                    if f.is_file() and f not in expected
                    and "__pycache__" not in f.parts   # 字节码缓存是运行产物，不算残留
                )
        if not changes and not extras:
            print("engine/ 与工作树一致，无漂移。")
            return 0
        if extras:
            print(f"engine/ 有 {len(extras)} 个工作树已不存在的残留文件（同步只增不删，确认后手动清理）：")
            for f in extras:
                print(f"  [残留] {f.relative_to(ROOT)}")
        if not changes:
            return 1
        print(f"engine/ 落后工作树 {len(changes)} 个文件：")
        for target, _body, group in changes:
            print(f"  [{group}] {target.relative_to(ROOT)}")
        return 1

    if not changes:
        print("engine/ 已是最新，无需同步。")
        return 0

    for target, body, group in changes:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        print(f"  [{group}] 更新 {target.relative_to(ROOT)}")
    print(f"同步完成：{len(changes)} 个文件（config.json / logs / data / python 未动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
