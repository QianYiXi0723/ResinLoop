"""批次三（接入层行为）回归：设备指纹签发三处修复。

  T1 旧缓存迁移只在环境一致时沿用 fp（缺环境标签的旧缓存视为直连签发）
  T2 代理变更 → 重新签发（不再被迁移分支吞掉）
  T3 getFp 降级指纹只供当轮、不落盘；签成后正常缓存
  T4 normalize_device_profile 缺可选字段不再让下游 KeyError（确定性兜底）

运行：python tests/rl_test_device_env.py
纪律：指纹缓存 _FP_CACHE_PATH 重定向到 %TEMP%；generate_fp 打桩——全程零真实请求。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.mihoyo_api as mia

TMP = Path(tempfile.mkdtemp(prefix="rl-devenv-"))
PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + (("（%s）" % detail) if detail else ""))


mia._FP_CACHE_PATH = TMP / "device_fp.json"
mia._PROXY = ""                      # 测试进程没有 _apply_mihoyo_query_config，直连态
_fp_calls = {"n": 0}


def _stub_fp(fp_value, degraded):
    def _generate(device_id, **kwargs):
        _fp_calls["n"] += 1
        mia._FP_LAST_DEGRADED = degraded
        return fp_value
    return _generate


DEVICE_ID = mia.identity_device_id()          # 真实身份文件只读使用
GLOBAL_ID = DEVICE_ID

# ---------- T1 旧缓存迁移（缺环境标签 → 视为直连，沿用 fp 零 getFp） ----------
(TMP / "device_fp.json").write_text(json.dumps({
    "genshin": {"device_id": "legacy-0123456789abcdef", "device_fp": "LEGACYFP0001"}
}), encoding="utf-8")
mia._FP_LAST_DEGRADED = False
mia.generate_fp = _stub_fp("SHOULD_NOT_SIGN", False)
_id, fp, ua = mia._get_device_info_locked("genshin")
saved = json.loads((TMP / "device_fp.json").read_text(encoding="utf-8"))["genshin"]
check("T1 旧缓存迁移沿用 fp 且零 getFp",
      fp == "LEGACYFP0001" and _fp_calls["n"] == 0
      and saved["device_id"] == GLOBAL_ID and saved["proxy"] == ""
      and "ua" in saved and "model" in saved,
      "fp=%s calls=%d" % (fp[:12], _fp_calls["n"]))

# ---------- T2 代理变更 → 重新签发（不再被迁移分支吞掉） ----------
mia._PROXY = "http://127.0.0.1:7890"          # 模拟用户改了代理
mia.generate_fp = _stub_fp("NEWFP0000000002", False)
_id, fp, ua = mia._get_device_info_locked("genshin")
saved = json.loads((TMP / "device_fp.json").read_text(encoding="utf-8"))["genshin"]
check("T2 代理变更触发重签并缓存新环境",
      fp == "NEWFP0000000002" and _fp_calls["n"] == 1
      and saved["device_fp"] == "NEWFP0000000002" and saved["proxy"] == "http://127.0.0.1:7890",
      "fp=%s" % fp[:12])

# ---------- T3 getFp 降级指纹不落盘；签成后正常缓存 ----------
mia._PROXY = "http://127.0.0.1:7891"          # 再换一次代理强制走签发
mia.generate_fp = _stub_fp("DEGRADED00000003", True)
_id, fp, ua = mia._get_device_info_locked("genshin")
mid = json.loads((TMP / "device_fp.json").read_text(encoding="utf-8")).get("genshin")
check("T3a 降级指纹只供当轮不落盘",
      fp == "DEGRADED00000003" and (mid is None or mid.get("device_fp") != "DEGRADED00000003"),
      "fp=%s cached=%s" % (fp[:12], (mid or {}).get("device_fp", "-")[:12]))

mia.generate_fp = _stub_fp("REALFP000000004", False)
_id, fp, ua = mia._get_device_info_locked("genshin")
after = json.loads((TMP / "device_fp.json").read_text(encoding="utf-8")).get("genshin")
check("T3b 下轮签成后正常缓存真实指纹",
      fp == "REALFP000000004" and after["device_fp"] == "REALFP000000004"
      and after["proxy"] == "http://127.0.0.1:7891")

# ---------- T4 normalize 缺可选字段 → 下游全部可跑 ----------
raw_paste = {
    "deviceModel": "TestPhone-5G",
    "deviceFingerprint": "samsung/b0q/b0q:13/TP1A.220624.014/b0quser:user/release-keys",
}
profile = mia.normalize_device_profile(raw_paste)
required = ("oaid", "sdk_version", "os_version", "screen_size",
            "display", "board", "brand", "manufacturer", "device",
            "device_type", "model", "device_info")
missing = [k for k in required if not profile.get(k)]
ok_no_key = True
try:
    ua_text = mia.build_user_agent(profile)
    ext = mia._build_ext_fields(profile)
    tag = mia._device_env_tag()
except KeyError as exc:
    ok_no_key = False
    missing.append("KeyError:%s" % exc)
check("T4 缺可选字段兜底且下游零 KeyError",
      not missing and ok_no_key
      and len(profile["oaid"]) == 64
      and profile["oaid"] == mia.normalize_device_profile(raw_paste)["oaid"],
      "missing=%s" % (missing or "无"))

# ---------- T5/T6 米游社昵称限频刷新（方案 A） ----------
import core.config as rc
import main as m

m._ROLES_STATE_PATH = TMP / ".roles_refresh"
cfg_path = TMP / "config_t5.json"
cfg_path.write_text(json.dumps({
    "accounts": [
        {"game": "starrail", "enabled": True, "nickname": "开拓者",
         "credential": {"cookie": "ltuid=10086; ltoken=ltok;", "uid": "101", "server": "cn_gf01"}},
    ],
    "query": {"provider": "miyoushe"},
}, ensure_ascii=False), encoding="utf-8")
config = rc.parse_config(json.loads(cfg_path.read_text(encoding="utf-8")), source=cfg_path)
config.config_path = cfg_path

roles_calls = {"n": 0}


def _fake_roles(ltuid, ltoken, cookie=""):
    roles_calls["n"] += 1
    return {"starrail": {"uid": "101", "server": "cn_gf01", "nickname": "星穹新名", "level": 70}}


mia.fetch_game_roles = _fake_roles
from core.models import GameStatus, TaskItem

statuses_ok = [GameStatus(game_id="starrail", game_name="崩坏：星穹铁道",
                          nickname="开拓者", error=None,
                          tasks=[TaskItem("每日实训", True, "1/1")])]
m._refresh_mihoyo_nicknames(config, statuses_ok)
after_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
state = json.loads((TMP / ".roles_refresh").read_text(encoding="utf-8"))
check("T5 昵称刷新写回且限频记账",
      after_cfg["accounts"][0]["nickname"] == "星穹新名"
      and "10086" in state and roles_calls["n"] == 1,
      "nickname=%s calls=%d" % (after_cfg["accounts"][0]["nickname"], roles_calls["n"]))

# T5b 24h 内第二次调用：直接跳过
m._refresh_mihoyo_nicknames(config, statuses_ok)
check("T5b 限频生效（24h 内不打接口）", roles_calls["n"] == 1, "calls=%d" % roles_calls["n"])

# T6 查询失败（风控）的账号：绝不打接口
(TMP / ".roles_refresh").unlink()
statuses_err = [GameStatus(game_id="starrail", game_name="崩坏：星穹铁道",
                           nickname="开拓者", error="接口 retcode=10041：风控拒绝",
                           tasks=[TaskItem("每日实训", False, "0/1")])]
m._refresh_mihoyo_nicknames(config, statuses_err)
check("T6 查询失败绝不加请求",
      roles_calls["n"] == 1 and not (TMP / ".roles_refresh").exists(),
      "calls=%d" % roles_calls["n"])


import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("=== rl_test_device_env.py: %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)))
sys.exit(0 if not FAIL else 1)
