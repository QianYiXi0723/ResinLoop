# -*- coding: utf-8 -*-
"""文案铁律守卫：用户可见文本不得出现「平台默认同账号」假设。

口径（2026-10-30 用户重申）：
  - 任何平台的任何游戏都可能绑定不同账号（米游社也是），不能写「一次扫码绑三款」；
  - 不写「米游社 1 次 / 森空岛每款 1 次」这类平台之间的扫码次数对比；
  - 不写分步扫码教学（点胶囊 → 勾选 → 扫码 → 红点变绿点）。

只扫**用户可见面**：README.md、发布用的 README_TXT、ui/index.html、ui/app.js。
代码注释 / docstring 里的机制描述不受限（既定口径：注释可写机制，面向用户的字不能预设同号）。

用法：python tests/rl_test_wording.py     （0 = 通过）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

PATTERNS = {
    "同平台默认同账号": r"同一(?:个)?(?:米游社)?账号的?\s*Cookie\s*通用",
    "一次扫码搞定多款": r"一次扫码[^。\n]{0,12}(?:三款|多个|全部)",
    "扫 N 次": r"扫\s*\d+\s*次",
    "1 次搞定": r"1\s*次搞定",
    "每款必须单独扫": r"每款(?:游戏)?(?:需要)?单独扫|勾几款就登录几款|每款一次扫码",
    "分步扫码教学": r"点[「\"]?(?:米游社|森空岛)[」\"]?\s*胶囊[^\n]{0,30}扫码|"
                    r"勾选[^\n]{0,20}(?:原神|星铁)[^\n]{0,20}扫码",
    "红点变绿点": r"红点会?变绿点|扫个码的事",
    "逐款扫码话术": r"逐款扫码|每款单独确认",
    "平台对比表": r"\|\s*\**(?:米游社|森空岛)\**\s*\|[^\n]*\|",
    "米游社=同账号": r"同一米游社账号",
    "反向暗示森空岛可不同号": r"可在不同账号下|支持两个不同账号",
}

# 改前真实样例（取自 v2.1.2 的 README 与小白说明），用于证明规则本身没被写废
SAMPLES = [
    "| **米游社** | 原神 / 崩铁 / 绝区零 | **1 次**（同一账号的 Cookie 通用） |",
    "点「米游社」胶囊 → 勾选原神/星铁/绝区零 → 手机米游社 App 扫码",
    "点「森空岛」胶囊 → 每款游戏单独扫一次码",
    "扫完页面上的红点会变绿点，横幅自动消失",
    "第二步：账号绑定（扫个码的事）",
]

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def hits(text):
    out = []
    for label, pat in PATTERNS.items():
        for m in re.finditer(pat, text):
            line = text.count("\n", 0, m.start()) + 1
            out.append((label, line, m.group(0)[:60]))
    return out


def read_ui(name):
    return (BASE / "ui" / name).read_text(encoding="utf-8")


def read_readme_txt():
    src = (BASE / "_build_release.py").read_text(encoding="utf-8")
    i = src.index('README_TXT = """')
    j = src.index('"""', i + len('README_TXT = """'))
    return src[i:j]


def main():
    print("=== 文案铁律守卫 ===")

    for sample in SAMPLES:
        check("规则自检命中：%s…" % sample[:18], bool(hits(sample)), "样例没被拦下")

    for name, text in [
        ("README.md", (BASE / "README.md").read_text(encoding="utf-8")),
        ("小白使用说明(README_TXT)", read_readme_txt()),
        ("ui/index.html", read_ui("index.html")),
        ("ui/app.js", read_ui("app.js")),
    ]:
        found = hits(text)
        detail = "; ".join(f"L{ln} {lb}: {fr}" for lb, ln, fr in found[:5])
        check(f"{name} 无平台同号假设", not found, detail)

    print(f"\n=== rl_test_wording.py: {PASS} PASS / {FAIL} FAIL ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
