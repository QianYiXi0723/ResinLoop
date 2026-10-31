# -*- coding: utf-8 -*-
"""mock 门卫 + 占位符凭证测试（2026-10-30 朋友机器实测暴露的两个缺陷）。

背景：新机器首启 → 出厂模板 config（provider=mock，cookie/token 全是
「请填写…」占位符）→ 守护进程照常跑了一轮，mock 假数据被写进巡检历史、
还编进周报推了出去；同时占位符凭证进了 HTTP 头后 'latin-1' 编码爆炸，
用户看到的是天书堆栈而不是"还没绑定"。

覆盖：
  M1  米游社：占位符 cookie 在 extract_credentials 就被拦下（不进网络），
      报错是可读中文，cred_kind 走 auth
  M2  米游社：真凭证格式照常通过（防误伤）
  M3  森空岛：占位符 token 在 _grant_code 就被拦下（不进网络）
  M4  daemon.run_check：provider=mock 直接跳过（返回 0，不调用 run_once）
  M5  daemon.run_check：provider=miyoushe 照常进入 run_once（防误伤）
  M6  daemon 周报：provider=mock 跳过发送
  M7  login_ui.has_credential 口径：占位符不算已绑定（_cred_filled）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import core.querier as Q  # noqa: E402
from core.config import AccountConfig, QueryConfig  # noqa: E402
from core.mihoyo_api import MiyousheAuthError, extract_credentials  # noqa: E402
from core.skland_api import SklandAuthError, _grant_code  # noqa: E402

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


def run_mihoyo_extract(cookie: str, game: str = "genshin"):
    acc = AccountConfig(game=game, nickname="n",
                        credential={"cookie": cookie, "uid": "100000001",
                                    "server": "cn_gf01"})
    try:
        return extract_credentials(acc), None
    except MiyousheAuthError as exc:
        return None, exc


# ---------------------------------------------------------------- M1 米游社占位符
res, exc = run_mihoyo_extract("ltuid=请填写; ltoken=请填写")
check("M1a 占位符 cookie 被拦下", res is None and exc is not None, str(exc))
check("M1b 报错是可读中文（点名扫码）", exc is not None and "扫码" in str(exc), str(exc))
check("M1c 不再是 latin-1 天书", exc is not None and "latin-1" not in str(exc), str(exc))

# 分字段填法的占位符也要拦
res2, exc2 = run_mihoyo_extract("")
# cookie 为空则回落 ltuid/ltoken（这里都空）→ 走"凭证不完整"，不挂
check("M1d 空 cookie 走凭证不完整分支（不进网络）",
      res2 is None and exc2 is not None and "凭证不完整" in str(exc2), str(exc2))

# ---------------------------------------------------------------- M2 真凭证防误伤
good = "ltuid=12345; ltoken=v2_abc; cookie_token=xyz; account_id=12345"
res3, exc3 = run_mihoyo_extract(good)
check("M2a 真凭证照常通过", exc3 is None and res3 is not None, str(exc3))
if res3:
    (ltuid, ltoken, uid, server, cookie) = res3
    check("M2b 字段完整", ltuid == "12345" and uid == "100000001" and server == "cn_gf01",
          f"{ltuid}/{uid}/{server}")

# ---------------------------------------------------------------- M3 森空岛占位符
try:
    _grant_code("请填写鹰角通行证Token")
    check("M3a 占位符 token 被拦下", False, "没有抛异常")
except SklandAuthError as exc:
    check("M3a 占位符 token 被拦下", True)
    check("M3b 报错是可读中文（点名扫码）", "扫码" in str(exc), str(exc))
except Exception as exc:  # 别的异常都算失败
    check("M3a 占位符 token 被拦下", False, repr(exc))

# ---------------------------------------------------------------- M4 daemon mock 门卫
import daemon  # noqa: E402
import main as main_mod  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="rl_mockguard_"))

# ---------------------------------------------------------------- 日志隔离
# 门卫测试会真跑 run_check/cmd_run，两者的 setup_logger 会把 handler 绑到
# 真实 logs/dailycheck.log——2026-10-31 实证：28 行测试残留写进了生产日志。
# 从源头把 core.logger.setup_logger 换成 no-op（run_check 内部是局部 import，
# 必须在 core.logger 模块上 patch），日志统一进 tmp。
import logging  # noqa: E402
import core.logger as _core_logger  # noqa: E402

_core_logger.setup_logger = lambda *a, **k: logging.getLogger("dailycheck")
_test_handler = logging.FileHandler(tmp / "test_isolated.log", encoding="utf-8")
_test_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"))
logging.getLogger("dailycheck").addHandler(_test_handler)
# main.py 在模块顶层绑定了 setup_logger 引用——只 patch core.logger 源头
# 对它无效（cmd_run 走的是已绑定引用），main 侧也要换成同款 no-op
main_mod.setup_logger = lambda *a, **k: logging.getLogger("dailycheck")

cfg_mock = tmp / "config_mock.json"
cfg_mock.write_text(json.dumps({
    "query": {"provider": "mock"},
    "accounts": [],
}, ensure_ascii=False), encoding="utf-8")

called = {"n": 0}
orig_run_once = main_mod.run_once
main_mod.run_once = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0
orig_path = daemon.CONFIG_PATH
try:
    daemon.CONFIG_PATH = cfg_mock
    code = daemon.run_check("测试 mock 门卫")
    check("M4a provider=mock 返回 0（补发记为已处理，不反复触发）", code == 0, str(code))
    check("M4b 没有真正跑检查", called["n"] == 0, str(called))
finally:
    daemon.CONFIG_PATH = orig_path
    main_mod.run_once = orig_run_once

# ---------------------------------------------------------------- M5 防误伤
cfg_real = tmp / "config_real.json"
cfg_real.write_text(json.dumps({
    "query": {"provider": "miyoushe"},
    "accounts": [],
}, ensure_ascii=False), encoding="utf-8")
called["n"] = 0
main_mod.run_once = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0
try:
    daemon.CONFIG_PATH = cfg_real
    code = daemon.run_check("测试防误伤")
    check("M5a provider=miyoushe 照常进入检查", called["n"] == 1, str(called))
    check("M5b 退出码来自 run_once", code == 0, str(code))
finally:
    daemon.CONFIG_PATH = orig_path
    main_mod.run_once = orig_run_once

# ---------------------------------------------------------------- M6 周报门卫
orig_weekly_digest = None
sent = {"n": 0}
try:
    daemon.CONFIG_PATH = cfg_mock
    # history.weekly_digest 若真有残留数据也不该发 —— 直接让 build_notifiers 抛错
    # 之前先验证 mock 门卫已提前返回（到不了 build_notifiers）
    code_path = daemon.run_weekly_report()
    check("M6 provider=mock 周报跳过（返回 False）", code_path is False, str(code_path))
finally:
    daemon.CONFIG_PATH = orig_path

# ---------------------------------------------------------------- M6b CLI 门卫
# python main.py 在 provider=mock 下拒绝运行（假数据不进历史/推送）；--mock 放行
import argparse  # noqa: E402

from main import cmd_run  # noqa: E402


def _cli_args(**kw):
    base = {"proxy": None, "force_alert": False, "force_silent": False,
            "debug": False, "dry_run": False, "mock": False}
    base.update(kw)
    return argparse.Namespace(**base)


rc = cmd_run(cfg_mock, _cli_args())
check("M6a CLI provider=mock 拒绝运行（退出码 1）", rc == 1, str(rc))
called["n"] = 0
main_mod.run_once = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0
try:
    rc2 = cmd_run(cfg_mock, _cli_args(mock=True))
    check("M6b CLI --mock 显式放行进入检查", called["n"] == 1 and rc2 == 0,
          f"called={called} rc={rc2}")
finally:
    main_mod.run_once = orig_run_once

# ---------------------------------------------------------------- M7 绑定页口径
from login_ui import _cred_filled  # noqa: E402

check("M7a 占位符 cookie 不算已绑定", _cred_filled("ltuid=请填写; ltoken=请填写") is False)
check("M7b 占位符 token 不算已绑定", _cred_filled("请填写鹰角通行证Token") is False)
check("M7c 空值不算已绑定", _cred_filled("") is False and _cred_filled(None) is False)
check("M7d 真凭证算已绑定", _cred_filled("ltuid=123; ltoken=v2_x") is True)
check("M7e 中间含「请填写」的真凭证不误伤？—— 含占位符一律视为未绑定",
      _cred_filled("ltoken=abc请填写def") is False)

# ---------------------------------------------------------------- M9 自愈
# provider=mock 但启用账号带凭证（旧版升级残留/手写配置）→ 不再一刀切空转：
# 本轮按 miyoushe 继续巡检（2026-10-31 朋友机器实证：这台机器此前永远空转，
# 游戏概览/周报/巡检全空）
cfg_mock_cred = tmp / "config_mock_cred.json"
cfg_mock_cred.write_text(json.dumps({
    "query": {"provider": "mock"},
    "accounts": [
        {"game": "genshin", "enabled": True,
         "credential": {"cookie": "ltuid=1; ltoken=v2_test"}},
    ],
}, ensure_ascii=False), encoding="utf-8")

called["n"] = 0
main_mod.run_once = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0
try:
    daemon.CONFIG_PATH = cfg_mock_cred
    code = daemon.run_check("测试 mock+凭证自愈")
    check("M9a mock+有凭证 → 自愈进入检查（不再空转）", called["n"] == 1, str(called))
    check("M9b 退出码来自 run_once", code == 0, str(code))
finally:
    daemon.CONFIG_PATH = orig_path

# ---------------------------------------------------------------- 汇总
print(f"\n==== 共 {PASS + FAIL} 项：PASS {PASS} / FAIL {FAIL} ====")
sys.exit(1 if FAIL else 0)
