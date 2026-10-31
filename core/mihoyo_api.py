"""米游社 game_record 实时便笺 HTTP 访问层（第 2 阶段新增）。

职责：把"米游社怎么调"这件事集中到一个文件，core/querier.py 里的
MiyousheQuerier 只负责把 data 字典翻译成 TaskItem 列表。

集中管理的常量（需求 3 / 4）：
    RECORD_BASE      实时便笺接口的域名（三款游戏共用，path 不同）
    ENDPOINTS        三款游戏各自的便笺接口 path
    DEFAULT_SERVERS  三款游戏各自默认的 server 区服参数
    COOKIE_FIELDS    凭证字段名（ltuid / ltoken），全局唯一定义处
    APP_VERSION / DS_SALT   请求头 x-rpc-app_version 与 DS 签名盐

所有未能逐字核实、需要实测确认的内容，一律以 TODO 标注，不编造。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import string
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode as _urlencode

from .config import AccountConfig

logger = logging.getLogger("dailycheck.mihoyo")

# --------------------------------------------------------------------------- #
# 集中常量
# --------------------------------------------------------------------------- #

#: 实时便笺接口域名（game_record 系列接口统一走 api-takumi-record）
RECORD_BASE = "https://api-takumi-record.mihoyo.com"

#: 三款游戏的便笺接口 path。以下三个路径均已通过"无凭证探测"实测确认存在
#: （未登录时分别返回 retcode -1 / -10001 / -100，而不是 404）。
#: 字段名与参数名的最终确认仍需带真实凭证跑一次，相关 TODO 见 parse_* 函数。
ENDPOINTS: Dict[str, str] = {
    "genshin": "/game_record/app/genshin/api/dailyNote",   # 原神实时便笺（GET，未登录返回 -1 param error）
    "starrail": "/game_record/app/hkrpg/api/note",         # 崩铁实时便笺（GET，未登录返回 -10001 invalid request）
    "zzz": "/event/game_record_zzz/api/zzz/note",          # 绝区零实时便笺（GET，未登录返回 -100 请先登录）
}

#: 默认区服参数。账号 credential 里可写 "server" 覆盖（如原神渠道服 cn_qd01）。
DEFAULT_SERVERS: Dict[str, str] = {
    "genshin": "cn_gf01",
    "starrail": "prod_gf_cn",
    "zzz": "prod_gf_cn",   # TODO(字段确认)：绝区零 server 值待带凭证实测确认
}

#: 米游社凭证字段名（需求 4：字段名集中配置，不散落）。
#: 支持两种填法（二选一）：
#:   1. credential 里直接给 {"ltuid": "...", "ltoken": "..."}
#:   2. credential 里给整串 {"cookie": "ltuid=...; ltoken=...; account_id=...; ..."}
#:      会用 COOKIE_REGEX 从整串里抽取下面这两个字段。
COOKIE_FIELDS: Tuple[str, str] = ("ltuid", "ltoken")
COOKIE_REGEX: Dict[str, re.Pattern] = {
    "ltuid": re.compile(r"ltuid\s*=\s*([^;\s]+)", re.IGNORECASE),
    "ltoken": re.compile(r"ltoken\s*=\s*([^;\s]+)", re.IGNORECASE),
}

#: 请求头 x-rpc-app_version / DS 盐配套版本。
#: 逐字抄自 GsCore `gsuid_core/utils/api/mys/tools.py` 的 mys_version = "2.102.1"
#: 与其盐表（K2 / LK2 / 22 / 25 四套）。GS 便笺用 "25"（即本文件的 DS_SALT）。
APP_VERSION = "2.102.1"

# --------------------------------------------------------------------------- #
# 设备档案（机型伪装）
# --------------------------------------------------------------------------- #
#
# 米游社按「设备指纹」判定风险：机型、系统版本、Build 号、OAID 必须自洽。
# 出厂身份来自 DEVICE_PRESETS 随机分配（每台电脑不同，见 device_identity.json）；
# 用户也可以在网页「设备伪装」里粘贴自己手机导出的真实档案，覆盖到
# logs/device_profile.json（该目录已被 .gitignore 排除）。
#
# 历史说明：早期版本用固定的单台真机档案 + logs/device_oaid.txt 覆盖 OAID，
# 那会让所有用户共用同一台设备。现在 OAID 每次分配身份时随机生成，
# device_oaid.txt 不再读取（留着也不会生效）。

#: logs/ 目录：设备身份、指纹缓存与真机档案都放这里，不进版本库
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

#: 完整设备档案的覆盖文件（网页「设备伪装」写入，用户真机档案走这里）
PROFILE_OVERRIDE_PATH = _LOGS_DIR / "device_profile.json"

#: 自动分配的设备身份（首次运行随机挑机型后固化）。
#: 与上面的「用户手动覆盖」分开存：手动覆盖优先，删掉它就回落到自动身份。
IDENTITY_PATH = _LOGS_DIR / "device_identity.json"

# ============================ 模块级锁 ============================
# 【位置死命令】这两个锁必须定义在文件上部、`DEVICE_MODEL = load_device_profile()`
# 之前。首次运行（无 device_identity.json）时模块级初始化就会走
# load_device_profile → create_identity → _save_identity → _update_identity，
# 那条链要拿 _IDENTITY_LOCK；若锁定义在文件末尾，模块尚未执行到那里，会抛
# NameError: name '_IDENTITY_LOCK' is not defined —— 表现为全新安装后
# /api/device 无响应、一切走米游社的查询全挂。不要为了「就近使用」把锁往下搬。

#: 设备身份串行锁：并行查询时，多个米游社系游戏同时走到 _get_device_info 会
#: 各自生成不同的 uuid/device_fp 并互覆缓存 —— 每次运行指纹都在变，正是风控
#: 雷区（服务器会看到"同一 IP 下的多台陌生设备"）。加锁后首线程签发、其余
#: 线程命中缓存，与串行版行为完全一致。
_DEVICE_LOCK = threading.RLock()

#: 身份文件（device_identity.json）读写锁：device_id / aaid / vaid 的
#: 认领、签发与迁移都是"读→判→写"，双重检查锁依赖它。
_IDENTITY_LOCK = threading.RLock()


#: 机型预设库 —— 首次运行随机挑一台，之后固定（固化在 logs/device_identity.json）。
#: 每项只写「不能派生」的 8 个字段，device_info / display / sdk_version / manufacturer
#: 全部由 _materialize_preset() 拼装，手写不出前后矛盾的环境。
#: 全部是公开可查的真实机型，不含任何真机或用户隐私数据。
DEVICE_PRESETS: Tuple[Dict[str, str], ...] = (
    {
        "label": "Xiaomi 14",
        "brand": "Xiaomi",
        "model": "23127PN0CC",
        "device": "houji",
        "board": "pineapple",
        "android": "14",
        "build_id": "UKQ1.231003.002",
        "display_id": "OS1.0.26.0.UNCCNXM",
        "screen_size": "1200x2670",
    },
    {
        "label": "Redmi K70 Pro",
        "brand": "Redmi",
        "model": "23117RK66C",
        "device": "manet",
        "board": "kalama",
        "android": "14",
        "build_id": "UKQ1.231003.002",
        "display_id": "OS1.0.12.0.UNKCNXM",
        "screen_size": "1440x3200",
    },
    {
        "label": "Redmi Note 12 Turbo",
        "brand": "Redmi",
        "model": "23049RAD8C",
        "device": "marble",
        "board": "taro",
        "android": "13",
        "build_id": "TKQ1.221114.001",
        "display_id": "V14.0.6.0.TMRCNXM",
        "screen_size": "1080x2400",
    },
    {
        "label": "OnePlus 11",
        "brand": "OnePlus",
        "model": "PHK110",
        "device": "salami",
        "board": "kalama",
        "android": "14",
        "build_id": "UKQ1.230924.001",
        "display_id": "R.16b1a8e-1",
        "screen_size": "1440x3216",
    },
    {
        "label": "Samsung Galaxy S23",
        "brand": "samsung",
        "model": "SM-S9110",
        "device": "dm1q",
        "board": "kalama",
        "android": "14",
        "build_id": "UP1A.231005.007",
        "display_id": "S9110ZHS3CXAB",
        "screen_size": "1080x2340",
    },
    {
        "label": "Xiaomi 13",
        "brand": "Xiaomi",
        "model": "2211133C",
        "device": "fuxi",
        "board": "kalama",
        "android": "14",
        "build_id": "UKQ1.231003.002",
        "display_id": "OS1.0.5.0.UMCCNXM",
        "screen_size": "1080x2400",
    },
    {
        "label": "Redmi K60",
        "brand": "Redmi",
        "model": "23013RK75C",
        "device": "mondrian",
        "board": "taro",
        "android": "14",
        "build_id": "UKQ1.231003.002",
        "display_id": "OS1.0.6.0.UMNCNXM",
        "screen_size": "1440x3200",
    },
)

#: Android 大版本 -> API level：UA 与 ext_fields 里的 sdkVersion 必须对得上
_ANDROID_SDK: Dict[str, str] = {
    "16": "36",
    "15": "35",
    "14": "34",
    "13": "33",
    "12": "32",
    "11": "30",
    "10": "29",
    "9": "28",
}

#: 品牌 -> 厂商名（Build 指纹首段一般就是品牌）
_MANUFACTURERS: Dict[str, str] = {
    "redmi": "Xiaomi",
    "xiaomi": "Xiaomi",
    "poco": "Xiaomi",
    "honor": "HONOR",
    "huawei": "HUAWEI",
    "oppo": "OPPO",
    "vivo": "vivo",
    "iqoo": "vivo",
    "oneplus": "OnePlus",
    "realme": "realme",
    "samsung": "samsung",
    "google": "Google",
    "meizu": "Meizu",
    "motorola": "motorola",
    "nokia": "HMD Global",
}


def _random_oaid() -> str:
    """随机 16 位小写十六进制 —— 每次「换机」重新生成，避免所有安装共用一个值。"""
    return "".join(random.choices("0123456789abcdef", k=16))


def _materialize_preset(preset: Dict[str, str], oaid: str = "") -> Dict[str, str]:
    """把机型预设的 8 个字段展开成 12 字段完整档案。

    派生规则与 _fingerprint_parts() 互为逆运算，保证「写进去什么就拼出什么」：
        device_info = brand/device/device:android/build_id/display_id:user/release-keys
        display     = device_display_id
        sdk_version = _ANDROID_SDK[android]

    唯一例外是 device_type：米游社 App 上报的是营销型号（如 23127PN0CC），
    不是 codename，所以它等于 model 而不是 device。
    """
    brand = preset["brand"]
    device = preset["device"]
    android = preset["android"]
    build_id = preset["build_id"]
    display_id = preset["display_id"]
    return {
        "brand": brand,
        "manufacturer": _MANUFACTURERS.get(brand.lower(), brand),
        "model": preset["model"],
        "device": device,
        "device_type": preset["model"],
        "board": preset["board"],
        "device_info": (
            f"{brand}/{device}/{device}:{android}/{build_id}/{display_id}:user/release-keys"
        ),
        "os_version": android,
        "sdk_version": _ANDROID_SDK.get(android, ""),
        "screen_size": preset["screen_size"],
        "display": f"{device}_{display_id}",
        "oaid": oaid or _random_oaid(),
    }


def _fingerprint_parts(device_info: str) -> Dict[str, str]:
    """从 Build 指纹里拆字段。

    标准格式：``brand/product/device:version/incremental:type/tags``，
    例如 ``Redmi/miro/miro:16/BP2A.250605.031.A3/OS3.0.305.0.WOMCNXM:user/release-keys``。
    """
    parts = (device_info or "").split("/")
    if len(parts) < 4:
        return {}

    device_ver = parts[2].split(":")
    result = {
        "brand": parts[0].strip(),
        "product": parts[1].strip(),
        "device": device_ver[0].strip(),
        "os_version": device_ver[1].strip() if len(device_ver) > 1 else "",
        "build_id": parts[3].strip(),
    }
    if len(parts) >= 5:
        result["display"] = parts[4].split(":")[0].strip()
    return {key: value for key, value in result.items() if value}


def normalize_device_profile(raw: Dict[str, Any]) -> Dict[str, str]:
    """把手机「设备信息」App 导出的 JSON 归一化成 DEVICE_MODEL 的字段。

    兼容两种输入：
      - App 导出的原始 JSON（``deviceModel`` / ``deviceFingerprint`` / ``androidVersion`` …）
      - 已经是 DEVICE_MODEL 形状的 JSON（``model`` / ``device_info`` / ``os_version`` …）

    只收录认识且非空的字段，其余忽略；缺失项由调用方用默认值补齐。
    机型与 Build 指纹是必需的 —— 少了它们无法拼出自洽的真机环境。
    """
    if not isinstance(raw, dict):
        raise ValueError("设备档案必须是 JSON 对象")

    flat: Dict[str, str] = {}
    for key, value in raw.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            flat[str(key).strip().lower()] = text

    def pick(*names: str) -> str:
        for name in names:
            got = flat.get(name.lower())
            if got:
                return got
        return ""

    device_info = pick("deviceFingerprint", "device_info", "fingerprint")
    derived = _fingerprint_parts(device_info)

    model = pick("deviceModel", "model", "device_model")
    brand = pick("brand", "deviceBrand") or derived.get("brand", "")
    device = pick("deviceName", "device", "device_name") or derived.get("device", "")
    board = pick("deviceBoard", "board", "device_board")
    os_version = (
        pick("androidVersion", "os_version", "release") or derived.get("os_version", "")
    )
    display = pick("display", "buildDisplay") or derived.get("display", "")
    oaid = pick("oaid")
    sdk_version = (
        pick("sdkVersion", "sdk_version", "sdkInt") or _ANDROID_SDK.get(os_version, "")
    )
    screen_size = pick("screenSize", "screen_size", "resolution")
    manufacturer = pick("manufacturer") or _MANUFACTURERS.get(brand.lower(), brand)

    # 可选字段兜底：「设备信息」App 的导出未必含 OAID/分辨率/SDK 等字段，而下游
    # （ext_fields / UA / 注册 body / 环境标签）全是硬下标访问，缺一个键三款游戏
    # 全部 KeyError。兜底值从档案内容确定性派生——同一次粘贴永远得到同一值，
    # 否则 env_tag 每次加载都在变，会引发指纹重签循环。
    if not oaid:
        digest = hashlib.md5(f"{device_info}|{model}".encode("utf-8")).hexdigest()
        oaid = digest + digest                       # 64 位 hex，与真机 OAID 同形
    if not os_version:
        os_version = "13"
    if not sdk_version:
        sdk_version = _ANDROID_SDK.get(os_version, "33")
    if not screen_size:
        screen_size = "2400x1080"
    if not display:
        display = model
    if not board:
        board = model
    if not brand:
        brand = manufacturer or "Android"
    if not manufacturer:
        manufacturer = brand or "Android"
    if not device:
        device = model

    if not model:
        raise ValueError(
            "没找到机型字段 deviceModel。请粘贴 App 导出的完整 JSON 文本。"
        )
    if not device_info:
        raise ValueError(
            "没找到 Build 指纹字段 deviceFingerprint。"
            "请确认复制的是「设备信息」App 的完整导出内容。"
        )

    pairs = {
        "brand": brand,
        "manufacturer": manufacturer,
        "model": model,
        "device": device,
        "device_type": model,
        "board": board,
        "device_info": device_info,
        "os_version": os_version,
        "sdk_version": sdk_version,
        "screen_size": screen_size,
        "display": display,
        "oaid": oaid,
    }
    return {key: value for key, value in pairs.items() if value}


def _read_identity_raw() -> Dict[str, Any]:
    """读身份文件的原始 JSON（读不到返回空字典，不抛异常）。"""
    try:
        raw = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _update_identity(**fields: Any) -> Dict[str, Any]:
    """合并字段写入 device_identity.json（原子写），保留既有键。

    version / label / created_at / profile 与 device_id / aaid / vaid /
    app_install_at / app_update_at 是平级键 —— 档案归档案，设备身份归身份。
    """
    with _IDENTITY_LOCK:
        data = _read_identity_raw()
        data.update(fields)
        try:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = IDENTITY_PATH.with_name(IDENTITY_PATH.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(IDENTITY_PATH)
        except OSError as exc:
            logger.warning("设备身份写入失败（本次仍生效，下次会重新随机）：%s", exc)
        return data


def _save_identity(label: str, profile: Dict[str, str]) -> None:
    """写基础四键（version/label/created_at/profile）。

    走 _update_identity 的合并路径 —— device_id / aaid / vaid 等平级字段
    不会被抹掉；create_identity 随后用 _update_identity 覆盖它们。
    """
    _update_identity(
        version=1,
        label=label,
        created_at=int(time.time()),
        profile=profile,
    )


def _generate_id(length: int = 64) -> str:
    """大写字母+数字随机串（GsCore `generate_ID` 同款，用于 aaid / vaid）。

    【位置死命令】必须在 create_identity 之前 —— 首次运行时模块级
    `load_device_profile()` → `create_identity()` 会调用它，定义在文件末尾
    会抛 NameError。本函数只依赖 random/string，放前面没有副作用。
    """
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def create_identity(exclude_label: str = "") -> Dict[str, str]:
    """随机挑一台机型固化为本机身份，返回完整档案。

    exclude_label 非空时避开该机型 —— 「换一台」不会换到同一台上去。
    """
    pool = [p for p in DEVICE_PRESETS if p["label"] != exclude_label]
    if not pool:
        pool = list(DEVICE_PRESETS)
    preset = random.choice(pool)
    profile = _materialize_preset(preset)
    _save_identity(preset["label"], profile)
    now_ms = int(time.time() * 1000)
    _update_identity(
        device_id=str(uuid.uuid4()).lower(),
        aaid=_generate_id(),
        vaid=_generate_id(),
        app_install_at=now_ms - 45 * 86400 * 1000,
        app_update_at=now_ms - 7 * 86400 * 1000,
    )
    logger.info(
        "设备身份：%s（%s / Android %s）",
        preset["label"],
        preset["model"],
        preset["android"],
    )
    return profile


def identity_label() -> str:
    """当前机型名：用户手动覆盖时为「自定义机型」，否则是预设 label。"""
    if PROFILE_OVERRIDE_PATH.exists():
        return "自定义机型"
    return str(_read_identity_raw().get("label") or "")


def identity_device_id() -> str:
    """全局唯一安装级 device_id：无则从既有缓存认领/生成，并固化进身份文件。

    双重检查锁：先无锁读（热路径），未命中再进锁迁移/生成。
    """
    did = str(_read_identity_raw().get("device_id") or "")
    if did:
        return did
    with _IDENTITY_LOCK:
        did = str(_read_identity_raw().get("device_id") or "")   # 双重检查
        if did:
            return did
        did = _adopt_legacy_device_id()
        _update_identity(device_id=did)
        return did


def _adopt_legacy_device_id() -> str:
    """迁移：从 fp/绑定缓存里认领一个出现频次最高的已注册 device_id。

    频次并列时取 fp 缓存 genshin 条目；两者皆无才生成新 uuid。
    有意取舍：保留一个已注册设备、让其余成为孤儿 —— 比"三台全部重新生成"
    更接近「一次签发、终身复用」，且全程零 getFp 调用。
    """
    fp_cache = _load_fp_cache()
    bind_cache = _load_binding_cache()
    counts: Dict[str, int] = {}
    for entry in list(fp_cache.values()) + list(bind_cache.values()):
        did = str((entry or {}).get("device_id") or "")
        if did:
            counts[did] = counts.get(did, 0) + 1
    if not counts:
        return str(uuid.uuid4()).lower()
    best = max(counts.values())
    tied = sorted(did for did, count in counts.items() if count == best)
    genshin_id = str((fp_cache.get("genshin") or {}).get("device_id") or "")
    if genshin_id in tied:
        return genshin_id
    return tied[0]


def _identity_ids() -> Tuple[str, str]:
    """(aaid, vaid)：缺失则生成并固化进身份文件（同一安装终身复用）。

    整段收进 _IDENTITY_LOCK：首次并发调用时只有一个线程生成并落盘，
    其余线程在锁内读到已落盘值 —— 消除"本次请求用了未落盘值"的瞬时不一致。
    """
    with _IDENTITY_LOCK:
        data = _read_identity_raw()
        aaid = str(data.get("aaid") or "")
        vaid = str(data.get("vaid") or "")
        if not aaid or not vaid:
            aaid = aaid or _generate_id()
            vaid = vaid or _generate_id()
            _update_identity(aaid=aaid, vaid=vaid)
        return aaid, vaid


def _identity_app_times() -> Tuple[int, int]:
    """(app_install_at, app_update_at) 毫秒；缺失按 created_at 确定性派生并固化。

    基准一旦写入即恒定 —— appXxxTimeDiff 随真实时间单调递增（每次都声称
    "刚刚安装"正是要修的语义错位）。保证 update >= install。
    整段收进 _IDENTITY_LOCK：并发首调用共享同一次派生，杜绝基准漂移。
    """
    with _IDENTITY_LOCK:
        data = _read_identity_raw()
        install = int(data.get("app_install_at") or 0)
        update = int(data.get("app_update_at") or 0)
        if install and update and update >= install:
            return install, update
        created_ms = int(data.get("created_at") or 0) * 1000
        now_ms = int(time.time() * 1000)
        base = created_ms or now_ms
        install = install or (base - 45 * 86400 * 1000)
        update = update or (base - 7 * 86400 * 1000)
        if update < install:
            update = install
        _update_identity(app_install_at=install, app_update_at=update)
        return install, update


def load_device_profile() -> Dict[str, str]:
    """用户覆盖 > 已固化身份 > 首次随机分配。

    1. ``logs/device_profile.json`` —— 用户手动粘贴的真机档案，优先级最高
    2. ``logs/device_identity.json`` —— 首次运行随机挑的机型，之后一直用这台
    3. 都没有 —— 随机挑一台并固化，保证同一台电脑每次启动都是同一个身份
       （每次启动都换机型会让 device_fp 漂移，反而更容易被风控盯上）
    """
    if PROFILE_OVERRIDE_PATH.exists():
        try:
            stored = json.loads(PROFILE_OVERRIDE_PATH.read_text(encoding="utf-8"))
            return normalize_device_profile(stored)
        except (OSError, ValueError) as exc:
            logger.warning("设备档案覆盖文件无效，已忽略：%s", exc)

    profile = _read_identity_raw().get("profile")
    if isinstance(profile, dict):
        try:
            return normalize_device_profile(profile)
        except ValueError:
            logger.warning("设备身份文件内容无效，将重新随机分配")

    return create_identity()


def shuffle_device_profile() -> Dict[str, str]:
    """换一台设备：重新随机机型 + 新 OAID，并清掉旧 device_fp 缓存。

    返回新档案。调用方负责 reload_device_profile() 让 UA 跟着刷新。
    """
    profile = create_identity(exclude_label=identity_label())
    clear_device_cache()
    try:
        _BINDING_CACHE_PATH.unlink()
        logger.info("换机：已清理设备注册缓存，下次查询将重新注册")
    except OSError:
        pass   # 缓存本就不存在，无需清理
    return profile


def build_user_agent(profile: Dict[str, str]) -> str:
    """按设备档案拼米游社 App 的 UA。

    机型、系统版本、Build 号必须与 ext_fields 完全一致，否则会被判定环境异常。
    """
    build_id = _fingerprint_parts(profile.get("device_info", "")).get("build_id", "")
    if not build_id:
        build_id = profile.get("build_id", "") or "UNKNOWN"
    return (
        f"Mozilla/5.0 (Linux; Android {profile['os_version']}; {profile['model']} "
        f"Build/{build_id}; wv)AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Version/4.0 Chrome/126.0.6478.133 Mobile Safari/537.36 "
        f"miHoYoBBS/{APP_VERSION}"
    )


def save_device_profile(raw: Dict[str, Any]) -> Dict[str, str]:
    """校验并写入设备档案覆盖文件，返回归一化后的档案。"""
    profile = normalize_device_profile(raw)
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".device_profile-", suffix=".tmp", dir=str(_LOGS_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_name, PROFILE_OVERRIDE_PATH)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return profile


def clear_device_profile() -> None:
    """恢复默认档案（删掉覆盖文件）。"""
    PROFILE_OVERRIDE_PATH.unlink(missing_ok=True)


def clear_device_cache() -> bool:
    """删掉 device_fp / device_id 缓存，强制下次按当前档案重新签发。

    返回是否真的删掉了文件（没有缓存时返回 False）。
    """
    existed = _FP_CACHE_PATH.exists()
    _FP_CACHE_PATH.unlink(missing_ok=True)
    return existed


#: 档案变化时的订阅者：其它模块把自己缓存的请求头刷新一下
_PROFILE_LISTENERS: List[Any] = []


def on_device_profile_change(callback: Any) -> None:
    """注册回调：设备档案重载后调用，用于刷新各模块缓存的请求头。"""
    if callback not in _PROFILE_LISTENERS:
        _PROFILE_LISTENERS.append(callback)


def reload_device_profile() -> Dict[str, str]:
    """重新读取覆盖文件，刷新模块级 DEVICE_MODEL / USER_AGENT 并通知订阅者。

    必须用"原地更新"而不是重新赋值：别的模块 ``from .mihoyo_api import DEVICE_MODEL``
    拿到的是同一个 dict 对象，原地更新才能一起生效。
    """
    global USER_AGENT

    fresh = load_device_profile()
    DEVICE_MODEL.clear()
    DEVICE_MODEL.update(fresh)
    USER_AGENT = build_user_agent(DEVICE_MODEL)

    for callback in list(_PROFILE_LISTENERS):
        try:
            callback(DEVICE_MODEL)
        except Exception:  # noqa: BLE001 - 订阅者出错不能影响主流程
            logger.warning("设备档案订阅回调失败（已忽略）", exc_info=True)
    return DEVICE_MODEL


#: 运行时生效的设备档案：默认值 + 本地覆盖
DEVICE_MODEL: Dict[str, str] = load_device_profile()

#: 米游社安卓 App 的 UA —— 由 DEVICE_MODEL 推导，换档案后自动跟着变
USER_AGENT = build_user_agent(DEVICE_MODEL)


#: GsCore 命中风控码后追加的挑战头（_CHALLENGE_META，按游戏区分）。
#: 它不解决问题，但能让服务端把请求识别为"App 内嵌 H5 的验证流程"，配合新指纹
#: 有概率放行；这是 GsCore 在 `_mys_request` 里 retcode ∈ _DEAD_CODE 时的标准动作。
CHALLENGE_META: Dict[str, Dict[str, str]] = {
    "genshin": {
        "x-rpc-challenge_game": "2",
        "x-rpc-page": "v4.1.5-ys_#ys",
        "x-rpc-tool-verison": "v4.1.5-ys",
    },
    "starrail": {
        "x-rpc-challenge_game": "6",
        "x-rpc-page": "v1.4.1-rpg_#/rpg",
        "x-rpc-tool-verison": "v1.4.1-rpg",
    },
    "zzz": {
        "x-rpc-challenge_game": "8",
        "x-rpc-page": "v1.0.14_#/zzz",
        "x-rpc-tool-verison": "v1.0.14-zzz",
    },
}

#: GsCore `_DEAD_CODE`：命中即视为设备/账号被风控，触发挑战头 + 重算 DS 重试一次。
RISK_RETCODES = (10035, 5003, 10041, 1034)

#: DS 签名盐。game_record 系列便笺接口使用 DS2 算法 + x4 盐（.so 提取，
#: 跨版本稳定），三款游戏统一适用，已实测通过：
#:   原神  /game_record/app/genshin/api/dailyNote  DS2+x4 -> 10001 Please login（DS 通过）
#:   崩铁  /game_record/app/hkrpg/api/note         DS1 被拒(-10001)，DS2+x4 通过
#:   绝区零 /event/game_record_zzz/api/zzz/note    DS2+x4 -> -100 请先登录（DS 通过）
DS_SALT = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"

#: 备用盐（同为 .so 提取的 DS2 盐，x4 失效时可试换）；以及 DS1 用的米游社 App 盐，
#: 注意 DS1 盐跟随 App 版本，必须与 APP_VERSION 配套（2.102.1 的 K2 盐）。
DS_SALT_X6 = "t0qEgfub6cvueAPgR5m9aQWWVciEer7v"
DS_SALT_DS1 = "lX8m5VO5at5JG7hR8hzqFwzyL5aB1tYo"

#: 通行证（passport）DS 盐，用于 deviceLogin / saveDevice（抄自 GsCore tools.py 的 "PD"）
DS_SALT_PASSPORT = "JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS"

#: 设备注册接口（把 device_id/fp 绑定到米游社账号，抄自 GsCore api.py）
BBS_API_BASE = "https://bbs-api.miyoushe.com"
DEVICE_LOGIN_URL = BBS_API_BASE + "/apihub/api/deviceLogin"
SAVE_DEVICE_URL = BBS_API_BASE + "/apihub/api/saveDevice"

#: 单次请求超时（秒）。TODO(配置化)：如需按账号覆盖，第 3 阶段再接 config。
REQUEST_TIMEOUT = 10


# --------------------------------------------------------------------------- #
# 全局运行开关：代理 / 设备模式（由 main 在加载配置后设置）
# --------------------------------------------------------------------------- #

#: 米游社相关请求统一使用的代理（None = 直连，沿用系统代理环境变量）
_PROXY: Optional[str] = None

#: 设备模式：auto（GsCore 原生 + 风控重试）/ fp（始终带设备头）/ gs（完全不带）
_DEVICE_MODE: str = "auto"


def _mask_proxy(url: str) -> str:
    """代理地址脱敏（把 user:pass@ 部分打码），用于日志。"""
    return re.sub(r"//[^/@]+@", "//***@", url)


def set_proxy(proxy: str) -> None:
    """设置米游社请求代理；空串表示直连。

    为什么要统一走代理：getFp 是按**出口 IP** 签发指纹的，如果只有 dailyNote
    走代理而 getFp 直连，签出来的仍是本机 IP 的黑指纹，等于没换。
    """
    global _PROXY
    value = (proxy or "").strip()
    if value and "://" not in value:
        # 允许只填 127.0.0.1:7890，自动补 http://
        value = "http://" + value
    _PROXY = value or None
    if _PROXY:
        logger.info("米游社请求将走代理：%s", _mask_proxy(_PROXY))


def get_proxy() -> str:
    """返回当前代理地址（空串 = 直连），供探针/日志使用。"""
    return _PROXY or ""


def set_device_mode(mode: str) -> str:
    """设置设备模式（auto / fp / gs），返回生效值。非法值回退 auto。"""
    global _DEVICE_MODE
    value = (mode or "auto").strip().lower()
    if value not in ("auto", "fp", "gs"):
        logger.warning("未知的 mihoyo_device_mode=%r，回退 auto", mode)
        value = "auto"
    _DEVICE_MODE = value
    logger.debug("米游社设备模式：%s", value)
    return value


def get_device_mode() -> str:
    return _DEVICE_MODE


def _proxies() -> Optional[Dict[str, str]]:
    """requests 的 proxies 参数（None 走直连）。"""
    if not _PROXY:
        return None
    return {"http": _PROXY, "https": _PROXY}

#: 常见 retcode 的可读解释。5003 的最终归因（2026-10-26 多轮实测，含 GsCore 方案）：
#:   - 请求头/DS/Cookie 均有效：getGameRecordCard 返回 retcode=0；
#:   - deviceLogin + saveDevice 能成功（retcode=0），说明设备注册链路通；
#:   - 但 dailyNote 仍稳定 5003/10041，且 getFp 对**同一出口 IP 永远返回同一个
#:     device_fp**（换 device_id/seed/aaid 都一样）——该 IP 已被米游社标记，
#:     签发的指纹天然带标记，换设备 ID 无效。
#:   结论：这是 IP/账号级风控（与 Hutao 文档一致：多设备共用通行证时无感验证
#:   基本无法成功，同 IP 频繁交互会被封禁）。换网络（如手机热点）会拿到新 fp。
RETCODE_HINTS: Dict[int, str] = {
    10001: "Cookie 无效或已过期（未登录）",
    -100: "未登录 / Cookie 不完整",
    1034: "触发风控，需要人机验证（换网络或等几小时再试；刚换过设备身份时也会触发）",
    10035: "触发风控验证（崩铁/绝区零的 1034 同族错误，待确认）",
    5003: "风控拒绝（与 1034/-999 同族；合成指纹被标记时稳定出现，需真实设备指纹或等风控冷却）",
    10041: "风控拒绝（同一网络频繁查询、或刚换过设备身份时常见，冷却后自行恢复）",
    -10003: "DS 签名校验失败（salt / app_version 过期）",
    -1: "接口返回未知错误",
}


class MiyousheQueryError(Exception):
    """米游社查询失败（网络 / HTTP / retcode / 字段缺失），由 main 捕获标记为查询失败。"""


class MiyousheAuthError(MiyousheQueryError):
    """凭证缺失 / 格式错误。"""


class MiyousheNotBoundError(MiyousheAuthError):
    """凭证有效，但这个米游社账号下**没有这款游戏的角色**（或还没绑定角色 UID）。

    典型场景：勾选了原神并扫码，但该账号从来没玩过原神 —— 登录本身是成功的，
    查不到数据只是因为没角色。它不该显示成红色「查询失败」，也不该触发
    「凭证失效」告警（凭证是好的），所以单独一档，等建号后自动生效。
    """


#: config.json 首启播种模板里的角色 UID 占位值（core/config.py 同值）。
#: 扫码登录会自动用角色列表回填真实 UID；查询时见到这个值说明还没绑定过角色。
PLACEHOLDER_UID = "100000000"


# --------------------------------------------------------------------------- #
# 凭证处理
# --------------------------------------------------------------------------- #


def extract_credentials(account: AccountConfig) -> Tuple[str, str, str, str, str]:
    """从账号配置里取出 (ltuid, ltoken, uid, server, cookie)。

    cookie 是**完整 Cookie 串**（有就原样发，像浏览器/GsCore 那样带上
    cookie_token / account_id / mid 等字段），没有则用 ltuid+ltoken 拼一个。
    只发 ltuid+ltoken 会丢掉 cookie_token，部分接口会因此判为"凭证不完整"。

    credential 支持两种填法：
        {"ltuid": "...", "ltoken": "...", "uid": "...", "server": "..."}
        {"cookie": "整串 Cookie", "uid": "...", "server": "..."}
    """
    credential = account.credential or {}

    ltuid = str(credential.get(COOKIE_FIELDS[0], "") or "").strip()
    ltoken = str(credential.get(COOKIE_FIELDS[1], "") or "").strip()
    cookie_raw = str(credential.get("cookie", "") or "").strip()

    # 出厂模板里的占位符（"请填写…"）是非空字符串，不拦的话会一路走进
    # HTTP 头，然后 'latin-1' codec 编码爆炸 —— 用户看到的是天书堆栈。
    # 这里提前识别成"还没绑定"，给一句能看懂的中文。
    if any("请填写" in part for part in (ltuid, ltoken, cookie_raw)):
        raise MiyousheAuthError(
            f"游戏 {account.game_name} 的米游社凭证还没绑定（仍是出厂占位符），"
            "请在「账号绑定」页勾选后扫码登录米游社"
        )

    if cookie_raw:
        parsed = parse_cookie_string(cookie_raw)
        ltuid = ltuid or parsed.get(COOKIE_FIELDS[0], "")
        ltoken = ltoken or parsed.get(COOKIE_FIELDS[1], "")

    if not (ltuid and ltoken):
        raise MiyousheAuthError(
            f"游戏 {account.game_name} 的登录凭证不完整，请重新扫码登录一次即可自动补全"
        )

    uid = str(credential.get("uid", "") or "").strip()
    if not uid or uid == PLACEHOLDER_UID:
        # 一句话：原因 + 出路。UID/回填是实现细节，不进用户可见文案
        # （游戏名也不重复——徽章/摘要行的前缀已经带游戏名）
        raise MiyousheNotBoundError("账号下没有这款游戏的角色，建号后自动生效")

    server = str(credential.get("server", "") or "").strip() or DEFAULT_SERVERS.get(account.game, "")
    if not server:
        raise MiyousheAuthError(f"游戏 {account.game_name} 没有识别到所在服务器，请重新扫码登录一次")

    cookie = cookie_raw or f"{COOKIE_FIELDS[0]}={ltuid}; {COOKIE_FIELDS[1]}={ltoken}"
    return ltuid, ltoken, uid, server, cookie


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """从整串 Cookie 字符串抽取 ltuid / ltoken。"""
    result: Dict[str, str] = {}
    for name, pattern in COOKIE_REGEX.items():
        match = pattern.search(raw)
        if match:
            result[name] = match.group(1).strip().strip('"')
    return result


# --------------------------------------------------------------------------- #
# 设备指纹（device_fp）：dailyNote 系接口的强制校验项
# --------------------------------------------------------------------------- #

#: 设备指纹生成接口（公开接口，无需凭证）。来源：GsCore / mystool 的实现。
DEVICE_FP_URL = "https://public-data-api.mihoyo.com/device-fp/api/getFp"

#: device_id / device_fp 本地缓存：每个游戏固定一套设备信息，避免每次运行
#: 都生成新设备触发风控。存 logs/ 下（已被 .gitignore 排除）。
_FP_CACHE_PATH = Path(__file__).resolve().parent.parent / "logs" / "device_fp.json"

#: 设备注册结果缓存：米游社账号（ltuid）维度记录"已把哪个 device_id 注册过"，
#: 避免每轮跑都重复调 deviceLogin / saveDevice。
_BINDING_CACHE_PATH = Path(__file__).resolve().parent.parent / "logs" / "mihoyo_binding.json"


def _generate_seed_id(length: int = 8) -> str:
    """n 位十六进制随机串（兼容旧调用）。"""
    return hex(random.randint(0, int("FF" * length, 16)))[2:]


def _generate_random_hex(length: int) -> str:
    """n 位小写十六进制随机串（GsCore `generate_seed` 同款）。"""
    return "".join(random.choices("0123456789abcdef", k=length))


def _generate_fp_locally(length: int = 13) -> str:
    """本地兜底 fp：13 位小写十六进制（GsCore `generate_random_fp` 同款）。"""
    return "".join(random.choices(string.digits + "abcdef", k=length))


def _build_ext_fields(profile: Dict[str, str]) -> str:
    """GsCore `BaseMysApi.generate_fp` 的 ext_fields 拼接（机型档案可替换）。

    这段是"真机环境"的全部秘密：把手机的 Build 指纹、主板、传感器读数、
    内存/存储容量、ROM 版本、运营商、网络类型、电量、安装时间等 53 个字段
    一次性报给 getFp，服务端才会签发"看起来像真机"的 device_fp。
    字段顺序、引号、数字/字符串类型都与 GsCore 完全一致（顺序错也可能被判定异常）。

    与 GsCore 的唯一差别：机型相关的 8 个值改为读传入的 profile
    （来自 DEVICE_PRESETS 随机分配的身份，或用户手动粘贴的真机档案），
    而不是 GsCore 写死的一加 PHK110。
    """
    random_data = random.randint(400000, 600000)
    random_data2 = random.randint(150000, 300000)
    now_ms = int(time.time() * 1000)
    aaid, vaid = _identity_ids()
    install_at, update_at = _identity_app_times()
    install_diff = str(now_ms - install_at)      # 保持字符串，类型不动
    update_diff = now_ms - update_at             # 保持整数，类型不动
    return (
        '{"proxyStatus":0,"isRoot":0,"romCapacity":"512",'
        '"deviceName":"私人手机",'
        f'"productName":"{profile["device"]}",'
        f'"romRemain":"491","hostname":"dg02-pool06-kvm82","screenSize":"{profile["screen_size"]}",'
        f'"isTablet":0,"aaid":"{aaid}","model":"{profile["model"]}",'
        f'"brand":"{profile["brand"]}","hardware":"qcom","deviceType":"{profile["device_type"]}",'
        '"devId":"REL","serialNumber":"unknown",'
        f'"sdCapacity":{random_data},"buildTime":"1717740969000","buildUser":"builder",'
        f'"simState":5,"ramRemain":"{random_data2}","appUpdateTimeDiff":{update_diff},'
        f'"deviceInfo":"{profile["device_info"]}","vaid":"{vaid}",'
        f'"buildType":"user","sdkVersion":"{profile["sdk_version"]}","ui_mode":"UI_MODE_TYPE_NORMAL",'
        '"isMockLocation":0,"cpuType":"arm64-v8a","isAirMode":0,"ringMode":1,'
        '"chargeStatus":1,'
        f'"manufacturer":"{profile["manufacturer"]}",'
        f'"emulatorStatus":0,"appMemory":"512","osVersion":"{profile["os_version"]}",'
        '"vendor":"中国联通","accelerometer":"-1.3004991x6.38764x7.19103",'
        f'"sdRemain":{random_data2},"buildTags":"release-keys",'
        '"packageName":"com.mihoyo.hyperion","networkType":"WiFi",'
        f'"oaid":"{profile["oaid"]}","debugStatus":1,"ramCapacity":"{random_data}",'
        '"magnetometer":"27.1084x-48.5804x-24.8758",'
        f'"display":"{profile["display"]}",'
        f'"appInstallTimeDiff":"{install_diff}","packageVersion":"{APP_VERSION}",'
        '"gyroscope":"-0.02543317x0.005725792x0.003195791","batteryStatus":50,'
        f'"hasKeyboard":0,"board":"{profile["board"]}"}}'
    )


#: generate_fp 最近一次是否走了本地降级（True=服务端没签成、用的是随机假指纹）。
#: 只在 _DEVICE_LOCK 内读写；降级指纹只供当轮查询、**不落盘**——落盘就永远不会
#: 重签，服务端没签过的指纹会一直用下去，风控不自愈。
_FP_LAST_DEGRADED = False


def generate_fp(
    device_id: str,
    profile: Optional[Dict[str, str]] = None,
    seed_id: Optional[str] = None,
    seed_time: Optional[str] = None,
) -> str:
    """调用 getFp 签发设备指纹 —— GsCore `BaseMysApi.generate_fp` 的 Python 翻译。

    与旧实现的关键差异（全部来自 GsCore）：
      - body.device_id 是**随机 16 位 hex**，真实设备 ID 放在 bbs_device_id；
      - platform="2"、app_name="bbs_cn"；
      - ext_fields 是完整 Android Build 环境（见 _build_ext_fields）；
      - 用同一套 UA / DS 常量，不额外加 header。
    """
    profile = profile or DEVICE_MODEL
    payload = {
        "device_id": _generate_random_hex(16),
        "seed_id": seed_id or str(uuid.uuid4()),
        "platform": "2",
        "seed_time": seed_time or str(int(time.time() * 1000)),
        "ext_fields": _build_ext_fields(profile),
        "app_name": "bbs_cn",
        "bbs_device_id": device_id,
        "device_fp": _generate_fp_locally(),
    }
    try:
        requests = _import_requests()
        response = requests.post(
            DEVICE_FP_URL,
            json=payload,
            headers={"User-Agent": USER_AGENT},
            proxies=_proxies(),
            timeout=REQUEST_TIMEOUT,
        )
        body = response.json() if response.status_code == 200 else {}
    except Exception as exc:
        logger.warning("getFp 请求失败（%s），降级为本地随机指纹（大概率触发风控）", exc)
        _FP_LAST_DEGRADED = True
        return _generate_fp_locally()

    data = body.get("data") or {}
    code = data.get("code")
    fp = str(data.get("device_fp") or "")
    if fp and code in (200, None):
        logger.debug("getFp 签发成功：%s…（code=%s）", fp[:8], code)
        _FP_LAST_DEGRADED = False
        return fp
    logger.warning(
        "getFp 未签发有效指纹：retcode=%s code=%s msg=%s，降级本地随机指纹",
        body.get("retcode"), code, str(data.get("msg") or "")[:80],
    )
    _FP_LAST_DEGRADED = True
    return _generate_fp_locally()


def generate_fake_fp(
    device_id: str,
    seed_id: Optional[str] = None,
    seed_time: Optional[str] = None,
) -> str:
    """GsCore `generate_fake_fp` 的移植：用当前 DEVICE_MODEL 档案签发指纹。

    GsCore 原函数是把固定的七个机型参数传给 `generate_fp`；这里改为传整份
    机型档案（DEVICE_MODEL 由 load_device_profile 决定：随机分配的身份或
    用户手动粘贴的真机档案）。
    """
    return generate_fp(
        device_id=device_id,
        profile=DEVICE_MODEL,
        seed_id=seed_id or str(uuid.uuid4()),
        seed_time=seed_time or str(int(time.time() * 1000)),
    )


def _load_fp_cache() -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(_FP_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_fp_cache(cache: Dict[str, Dict[str, str]]) -> None:
    try:
        _FP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FP_CACHE_PATH.with_name(_FP_CACHE_PATH.name + ".tmp")
        tmp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(_FP_CACHE_PATH)   # 原子替换：并行读取方永远不会读到半截 JSON
    except OSError as exc:
        logger.warning("设备指纹缓存写入失败（不影响本次运行）：%s", exc)


def _load_binding_cache() -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(_BINDING_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_binding_cache(cache: Dict[str, Dict[str, str]]) -> None:
    try:
        _BINDING_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BINDING_CACHE_PATH.with_name(_BINDING_CACHE_PATH.name + ".tmp")
        tmp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(_BINDING_CACHE_PATH)
    except OSError as exc:
        logger.warning("设备注册缓存写入失败（不影响本次运行）：%s", exc)


def _get_device_info(game_id: str) -> Tuple[str, str, str]:
    """取（或生成并缓存）某游戏的 (device_id, device_fp, user_agent)。

    并行查询下必须串行化：见 _DEVICE_LOCK 注释。
    """
    with _DEVICE_LOCK:
        return _get_device_info_locked(game_id)


def _get_device_info_locked(game_id: str) -> Tuple[str, str, str]:
    """_get_device_info 的实体，必须在 _DEVICE_LOCK 内调用。

    缓存条目同时记录 UA / 代理 / 机型档案：
      - 代理一变，出口 IP 就变了，必须重新签发指纹（getFp 按 IP 签发）；
      - UA 或机型档案变了也要重签，否则服务端会看到"环境不一致"；
      - device_id 恒为全局身份（identity_device_id），迁移时只换 id 不换 fp。
    """
    cache = _load_fp_cache()
    entry = cache.get(game_id) or {}
    env_tag = _device_env_tag()
    device_id = identity_device_id()
    if (
        entry.get("device_id") == device_id
        and entry.get("device_fp")
        and all(entry.get(key) == value for key, value in env_tag.items())
    ):
        return entry["device_id"], entry["device_fp"], USER_AGENT

    # 旧缓存迁移（阶段 B）：device_id 换成全局身份，device_fp 原样保留（迁移零 getFp）。
    # 仅当「指纹签发环境与当前环境一致」才沿用——旧代码的缓存没有环境标签，那个年代
    # 只可能是直连（proxy=""）签发的；带环境标签的按标签比。代理/UA/机型任何一项
    # 对不上都必须落到重签路径：否则旧 IP 签的指纹配着新代理永不过期，风控不自愈。
    if entry.get("device_fp"):
        entry_proxy = entry.get("proxy", "")   # 缺 proxy 标签的旧缓存视为直连签发
        env_mismatch = (
            entry_proxy != (_PROXY or "")
            or ("ua" in entry and entry["ua"] != USER_AGENT)
            or ("model" in entry and entry["model"] != DEVICE_MODEL["model"])
        )
        if not env_mismatch:
            entry["device_id"] = device_id
            entry.update(env_tag)
            cache[game_id] = entry
            _save_fp_cache(cache)
            logger.info(
                "%s 设备身份迁移：device_id=%s…（沿用原 device_fp，未重新签发）",
                game_id, device_id[:8],
            )
            return device_id, entry["device_fp"], USER_AGENT
        logger.info("%s 设备环境变化（代理/UA/机型），重新签发指纹", game_id)

    # 全新安装 / 环境变化：按全局身份签发配套指纹
    device_fp = generate_fake_fp(device_id)
    if _FP_LAST_DEGRADED:
        # getFp 没签成、本轮用的是本地随机指纹：只供本轮、不写缓存（写进去
        # 就永久复用）；下轮进这里重试签发，签成之日即缓存真实指纹之时。
        logger.warning(
            "%s 本轮使用降级随机指纹（未缓存，下轮重试 getFp 签发）", game_id
        )
        return device_id, device_fp, USER_AGENT
    cache[game_id] = {
        "device_id": device_id,
        "device_fp": device_fp,
        **env_tag,
    }
    _save_fp_cache(cache)
    logger.info(
        "已为 %s 生成并缓存真机指纹（机型=%s，%s，代理=%s）",
        game_id, DEVICE_MODEL["model"], _FP_CACHE_PATH.name,
        _mask_proxy(_PROXY) if _PROXY else "直连",
    )
    return device_id, device_fp, USER_AGENT


# 注：`_DEVICE_LOCK` / `_IDENTITY_LOCK` 定义在文件上部（已 import threading 之后、
# DEVICE_MODEL 模块级初始化之前）。**不要在此处重新定义** —— 首次运行时
# 模块级 load_device_profile() 就要用 _IDENTITY_LOCK，定义在文件末尾必然 NameError。


def _device_env_tag() -> Dict[str, str]:
    """指纹缓存的环境标签：任一变化都要重新签发，避免"环境不一致"。"""
    return {
        "ua": USER_AGENT,
        "proxy": _PROXY or "",
        "device_info": DEVICE_MODEL["device_info"],
        "model": DEVICE_MODEL["model"],
        "oaid": DEVICE_MODEL["oaid"],
        "os_version": DEVICE_MODEL["os_version"],
    }


def bind_device(
    device_id: str, games: Optional[Sequence[str]] = None
) -> Dict[str, str]:
    """把"扫码登录用的设备 ID"绑定到指定游戏（默认三款）。

    扫码登录时米游社服务端看到的设备就是 device_id；之后 dailyNote 用同一个
    device_id + 为其新签发的 device_fp，做到"登录设备 = 查询设备"。

    games 传子集时**只重签这几款**，其余游戏的 device_id / device_fp 原样保留。
    「只登录某一款」时很关键：若把三款一起重签，那些仍挂在另一个米游社账号
    下的游戏会平白换一次设备身份，反而可能触发风控。
    """
    global_id = identity_device_id()
    if device_id != global_id:
        logger.warning(
            "扫码设备 %s… 已被全局身份 %s… 取代，按全局身份重签",
            device_id[:8], global_id[:8],
        )
        device_id = global_id
    targets = tuple(games) if games else ("genshin", "starrail", "zzz")
    with _DEVICE_LOCK:   # 扫码重签与并行查询共用一把锁，避免指纹互相覆盖
        cache = _load_fp_cache()
        fps: Dict[str, str] = {}
        env_tag = _device_env_tag()
        for game_id in targets:
            device_fp = generate_fake_fp(device_id)
            if _FP_LAST_DEGRADED:
                # 与自动签发同一原则：降级随机指纹不落盘（下轮自动签发会重试）
                logger.warning("%s 扫码重签遇到 getFp 降级，本轮指纹未缓存", game_id)
            else:
                cache[game_id] = {
                    "device_id": device_id,
                    "device_fp": device_fp,
                    **env_tag,
                }
            fps[game_id] = device_fp
        _save_fp_cache(cache)
    logger.info(
        "已将扫码设备 %s… 绑定到 %s 并重签指纹", device_id[:8], "、".join(targets)
    )
    return fps


# --------------------------------------------------------------------------- #
# 设备注册（deviceLogin / saveDevice）—— GsCore device_login_and_save 的 Python 版
# --------------------------------------------------------------------------- #


def _passport_ds(body_text: str, query: str = "") -> str:
    """通行证 DS（DS1 变体，抄自 GsCore generate_passport_ds）。

    md5(f"salt={PD}&t={t}&r={r}&b={body}&q={query}")，r 为 6 位大小写字母。
    """
    timestamp = str(int(time.time()))
    random_str = "".join(random.choices(string.ascii_letters, k=6))
    digest = hashlib.md5(
        f"salt={DS_SALT_PASSPORT}&t={timestamp}&r={random_str}&b={body_text}&q={query}".encode()
    ).hexdigest()
    return f"{timestamp},{random_str},{digest}"


def device_login_and_save(
    ltuid: str, ltoken: str, device_id: str, device_fp: str, cookie: str = ""
) -> Dict[str, Any]:
    """把设备注册到米游社账号 —— GsCore `device_login_and_save` 的 1:1 移植。

    依次 POST：
        https://bbs-api.miyoushe.com/apihub/api/deviceLogin
        https://bbs-api.miyoushe.com/apihub/api/saveDevice

    关键点（与 GsCore 完全一致）：
      - body 的 device_name 是 `品牌+型号`（无空格），os_version 固定 "33"，
        platform "Android"，registration_id 为 19 位 hex；
      - 请求头的 x-rpc-device_id / fp / name / model 用的就是查询便笺那套设备，
        保证"注册设备 = 查询设备"；
      - DS 用通行证盐（PD）按 body 原文签名；
      - 走同一个代理（换 IP 时注册与查询必须同源，否则设备绑定无意义）。

    Returns:
        {"login": 返回JSON, "save": 返回JSON}；网络失败抛 MiyousheQueryError。
    """
    requests = _import_requests()
    brand = DEVICE_MODEL["brand"]
    model_name = DEVICE_MODEL["model"]
    body = {
        "app_version": APP_VERSION,
        "device_id": device_id,
        "device_name": f"{brand}{model_name}",
        "os_version": DEVICE_MODEL["sdk_version"],
        "platform": "Android",
        "registration_id": _generate_random_hex(19),
    }
    # GsCore 用 json.dumps(body)（默认分隔符）计算 DS，这里保持同样的字节序列。
    body_text = json.dumps(body)
    headers = {
        "x-rpc-app_version": APP_VERSION,
        "X-Requested-With": "com.mihoyo.hyperion",
        "User-Agent": USER_AGENT,
        "x-rpc-client_type": "5",
        "x-rpc-device_id": device_id,
        "x-rpc-device_fp": device_fp,
        "x-rpc-device_name": f"{brand} {model_name}",
        "x-rpc-device_model": model_name,
        "x-rpc-csm_source": "myself",
        "Referer": "https://app.mihoyo.com",
        "Host": "bbs-api.miyoushe.com",
        "DS": _passport_ds(body_text),
        "Cookie": cookie or f"{COOKIE_FIELDS[0]}={ltuid}; {COOKIE_FIELDS[1]}={ltoken}",
        "Content-Type": "application/json",
    }

    results: Dict[str, Any] = {}
    for key, url in (("login", DEVICE_LOGIN_URL), ("save", SAVE_DEVICE_URL)):
        try:
            resp = requests.post(
                url,
                data=body_text.encode("utf-8"),
                headers=headers,
                proxies=_proxies(),
                timeout=REQUEST_TIMEOUT,
            )
            payload = resp.json()
        except Exception as exc:
            raise MiyousheQueryError(f"{key} 设备注册请求失败：{exc}") from exc
        results[key] = payload
        logger.debug("[device] %s 原始返回（脱敏）：%s", key, _sanitize_for_log(payload)[:300])
    return results


#: 设备注册并发纪律：绑定缓存文件读写串行；同 账号+游戏 的注册按 key 串行。
_BINDING_FILE_LOCK = threading.Lock()
_BINDING_LOCKS: Dict[str, threading.Lock] = {}
_BINDING_LOCKS_GUARD = threading.Lock()


def _binding_lock(cache_key: str) -> threading.Lock:
    """取某 cache_key 专属的注册锁（无则创建；不同 key 不互相阻塞）。"""
    with _BINDING_LOCKS_GUARD:
        lock = _BINDING_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _BINDING_LOCKS[cache_key] = lock
        return lock


def ensure_device_bound(
    ltuid: str, ltoken: str, device_id: str, device_fp: str, cookie: str = "", game_id: str = ""
) -> bool:
    """确保当前设备已注册到该米游社账号（每个 账号+游戏+设备+代理 只做一次）。

    并发纪律：文件读写用 _BINDING_FILE_LOCK 串行（防互相覆盖），
    同 账号+游戏 的注册用 per-key 锁串行（网络调用在文件锁外，不阻塞别的 key）。
    设备注册是"锦上添花"：失败只记警告，绝不阻断后续 dailyNote 查询。
    """
    cache_key = f"{ltuid}:{game_id}" if game_id else str(ltuid)
    proxy_tag = _PROXY or ""

    def _registered(cache: Dict[str, Any]) -> bool:
        entry = cache.get(cache_key) or {}
        return (
            entry.get("device_id") == device_id
            and entry.get("ok")
            and entry.get("proxy") == proxy_tag
        )

    with _BINDING_FILE_LOCK:
        if _registered(_load_binding_cache()):
            return True

    with _binding_lock(cache_key):
        with _BINDING_FILE_LOCK:
            if _registered(_load_binding_cache()):   # 双重检查：等锁期间可能已被别线程注册
                return True
        try:
            results = device_login_and_save(ltuid, ltoken, device_id, device_fp, cookie=cookie)
        except MiyousheQueryError as exc:
            logger.warning("设备注册失败（不影响查询）：%s", exc)
            return False

        login_code = (results.get("login") or {}).get("retcode")
        save_code = (results.get("save") or {}).get("retcode")
        logger.info("设备注册结果：deviceLogin retcode=%s，saveDevice retcode=%s", login_code, save_code)
        ok = login_code in (0, 200) and save_code in (0, 200)
        with _BINDING_FILE_LOCK:
            cache = _load_binding_cache()   # 重读：等网络期间别的 key 可能已写盘
            cache[cache_key] = {"device_id": device_id, "ok": ok, "proxy": proxy_tag}
            _save_binding_cache(cache)
        return ok


# --------------------------------------------------------------------------- #
# DS 签名与请求头
# --------------------------------------------------------------------------- #


def build_ds(salt: Optional[str] = None, query: Optional[Dict[str, Any]] = None, body: str = "") -> str:
    """构造 DS 签名头。

    - query 不为 None 时走 **DS2**（本脚本默认）：`t,r,md5(salt=..&t=..&r=..&b={body}&q={sorted query})`。
      r 取 100000~200000 的随机整数（社区通行做法）。
    - query 为 None 时退回 **DS1**：`t,r,md5(salt=..&t=..&r=..)`，r 为 6 位随机串。
      DS1 盐跟随米游社 App 版本（DS_SALT_DS1 <-> APP_VERSION）。

    实测记录（2026-10-26，假 Cookie 探针，DS 通过的特征是 retcode=10001/-100 "请登录"）：
    崩铁便笺拒绝 DS1（-10001 invalid request），DS2+x4 三款游戏全部通过。
    """
    salt = salt or DS_SALT
    if not salt:
        raise MiyousheAuthError(
            "DS_SALT 尚未配置：米游社接口需要 DS 签名盐，"
            "请在 core/mihoyo_api.py 顶部的 DS_SALT / APP_VERSION 常量里填入当前有效值"
            "（获取方式见 README「米游社接入」一节）"
        )
    timestamp = str(int(time.time()))
    if query is None:
        random_str = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
        digest = hashlib.md5(f"salt={salt}&t={timestamp}&r={random_str}".encode()).hexdigest()
        return f"{timestamp},{random_str},{digest}"

    # DS2：query 串按参数**实际发送顺序**拼接（requests 会按 dict 插入顺序发），
    # GsCore 的 `get_ds_token` 也是 `"&".join(f"{k}={v}" for k,v in params.items())`。
    # GET 请求 body 为空串。
    query_string = "&".join(f"{k}={v}" for k, v in query.items())
    random_str = str(random.randint(100000, 200000))
    digest = hashlib.md5(
        f"salt={salt}&t={timestamp}&r={random_str}&b={body}&q={query_string}".encode()
    ).hexdigest()
    return f"{timestamp},{random_str},{digest}"


def build_headers(
    ltuid: str,
    ltoken: str,
    query: Optional[Dict[str, Any]] = None,
    device_id: str = "",
    device_fp: str = "",
    user_agent: str = "",
    cookie: str = "",
    with_device: bool = True,
) -> Dict[str, str]:
    """构造 game_record 接口的请求头 —— 基础部分逐字对齐 GsCore `BaseMysApi._HEADER`。

    GsCore 的六个基础头：
        x-rpc-app_version / X-Requested-With / User-Agent /
        x-rpc-client_type / Referer / Origin
    加上 Cookie 与 DS 后就是一次完整的便笺请求。

    设备头（x-rpc-device_id / fp / name / model）由 GsCore 在 `_mys_request`
    里按需注入（有 uid/role_id 且缺设备头时自动补），因此这里做成可选：
    with_device=False 时只发基础头，用于验证"设备指纹到底有没有用"。
    """
    headers = {
        "x-rpc-app_version": APP_VERSION,
        "X-Requested-With": "com.mihoyo.hyperion",
        "User-Agent": user_agent or USER_AGENT,
        "x-rpc-client_type": "5",
        "Referer": "https://webstatic.mihoyo.com/",
        "Origin": "https://webstatic.mihoyo.com/",
        "DS": build_ds(query=query),
        "Cookie": cookie or f"{COOKIE_FIELDS[0]}={ltuid}; {COOKIE_FIELDS[1]}={ltoken}",
    }
    if with_device and device_id:
        headers["x-rpc-device_id"] = device_id
        headers["x-rpc-device_name"] = f"{DEVICE_MODEL['brand']} {DEVICE_MODEL['model']}"
        headers["x-rpc-device_model"] = DEVICE_MODEL["model"]
        if device_fp:
            headers["x-rpc-device_fp"] = device_fp
    return headers


# --------------------------------------------------------------------------- #
# 请求与响应处理
# --------------------------------------------------------------------------- #


def _import_requests():
    """惰性导入 requests，缺失时给出可执行的修复提示（与 notifier 保持一致）。"""
    try:
        import requests  # noqa: PLC0415 - 故意延迟导入
    except ImportError as exc:
        raise MiyousheQueryError("缺少依赖 requests，请先执行：pip install -r requirements.txt") from exc
    return requests


#: 调试日志里敏感字段的名字（命中即脱敏）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|cookie|ltuid|ltoken|account_id|mid|stoken|login_ticket)", re.IGNORECASE
)
#: 调试日志单条最大长度
_DEBUG_MAX_CHARS = 1500


def _sanitize_for_log(obj: Any) -> str:
    """把响应/请求体转成脱敏、截断后的字符串，用于 --debug 日志。

    脱敏规则：JSON 里键名含 token/cookie/ltuid 等敏感词的值替换为 ***；
    另外把请求头 Cookie 串里的值整体打码。响应 data 里正常不含凭证，
    这里只是兜底，防止未来接口把用户信息混进返回体。
    """

    def mask(value: Any) -> str:
        return "***"

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (mask(v) if _SENSITIVE_KEY_PATTERN.search(str(k)) else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    try:
        text = json.dumps(walk(obj), ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= _DEBUG_MAX_CHARS else text[:_DEBUG_MAX_CHARS] + "…(截断)"


class _RetcodeError(MiyousheQueryError):
    """内部用：保留 retcode，便于上层判断是否属于风控码并决定重试。"""

    def __init__(self, retcode: Any, message: str, payload: Any = None):
        super().__init__(message)
        self.retcode = retcode
        self.payload = payload


def _daily_note_once(
    game_id: str,
    url: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """发一次便笺请求；retcode != 0 抛 _RetcodeError（带 retcode 供重试判断）。"""
    requests = _import_requests()
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            proxies=_proxies(),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # 连接失败、超时、DNS、代理不可用等网络层异常
        raise MiyousheQueryError(f"网络请求失败：{exc}") from exc

    if response.status_code != 200:
        logger.debug("[%s] HTTP %s 原始响应：%s", game_id, response.status_code, response.text[:_DEBUG_MAX_CHARS])
        raise MiyousheQueryError(f"米游社接口暂时没有响应（HTTP {response.status_code}），请稍后再试；详情见运行日志")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.debug("[%s] 非 JSON 原始响应：%s", game_id, response.text[:_DEBUG_MAX_CHARS])
        raise MiyousheQueryError("米游社返回了无法解析的数据，请稍后再试；详情见运行日志") from exc

    # 调试日志：无论成功失败，--debug 模式都打印脱敏后的原始返回（用户亲眼验证数据来源）
    logger.debug("[%s] 原始返回 JSON（脱敏）：%s", game_id, _sanitize_for_log(payload))

    retcode = payload.get("retcode")
    if retcode != 0:
        hint = RETCODE_HINTS.get(retcode, "")
        message = payload.get("message") or ""
        detail = "；".join(part for part in (hint, str(message)) if part) or str(payload)[:120]
        raise _RetcodeError(retcode, f"接口 retcode={retcode}：{detail}", payload)

    data = payload.get("data")
    if not isinstance(data, dict):
        raise MiyousheQueryError(f"响应缺少 data 字典，实际为：{type(data).__name__}")

    return data


def request_daily_note(
    game_id: str,
    ltuid: str,
    ltoken: str,
    uid: str,
    server: str,
    base_url: Optional[str] = None,
    cookie: str = "",
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """调用某游戏的实时便笺接口，校验 retcode 后返回 data 字典。

    Args:
        base_url: 覆盖接口域名（测试用）；None 时读模块常量 RECORD_BASE。
        cookie: 完整 Cookie 串（credential.cookie 原样）；空则用 ltuid+ltoken 拼。
        mode: 覆盖设备模式（auto/fp/gs），None 时用全局设置。

    GsCore 式重试（mode=auto）：
        命中 5003/10041/1034/10035 时，追加 `x-rpc-challenge_game` /
        `x-rpc-page` / `x-rpc-tool-verison` 三个挑战头，重新计算 DS 后再发一次。
        这是 GsCore `_mys_request` 里 `_DEAD_CODE` 分支的标准动作。

    Raises:
        MiyousheQueryError: 网络异常 / HTTP 非 200 / 响应非 JSON / retcode != 0 / 端点未配置。
    """
    base_url = (base_url or RECORD_BASE).rstrip("/")
    endpoint = ENDPOINTS.get(game_id)
    if endpoint is None:
        raise MiyousheQueryError(f"游戏 {game_id} 未配置米游社便笺接口端点")

    effective_mode = (mode or _DEVICE_MODE).lower()
    params: Dict[str, Any] = {"role_id": uid, "server": server}
    url = base_url + endpoint

    device_id = device_fp = user_agent = ""
    with_device = effective_mode != "gs"
    if with_device:
        device_id, device_fp, user_agent = _get_device_info(game_id)
        # 首次使用该设备（或换了代理）时，把它注册到米游社账号（GsCore 思路）。
        # 失败只记警告，不影响查询。
        ensure_device_bound(ltuid, ltoken, device_id, device_fp, cookie=cookie, game_id=game_id)

    headers = build_headers(
        ltuid, ltoken, query=params,
        device_id=device_id, device_fp=device_fp, user_agent=user_agent,
        cookie=cookie, with_device=with_device,
    )

    logger.debug(
        "[%s] 请求 %s?%s（模式=%s，device_id=%s，device_fp=%s，代理=%s）",
        game_id, endpoint, _urlencode(params), effective_mode,
        (device_id[:8] + "…") if device_id else "-",
        (device_fp[:8] + "…") if device_fp else "-",
        _mask_proxy(_PROXY) if _PROXY else "直连",
    )

    try:
        return _daily_note_once(game_id, url, params, headers)
    except _RetcodeError as exc:
        if effective_mode != "auto" or exc.retcode not in RISK_RETCODES:
            raise
        logger.warning(
            "[%s] 命中风控 retcode=%s，按 GsCore 方式补 challenge 头并重算 DS 重试一次",
            game_id, exc.retcode,
        )

    # GsCore `_mys_request` 的风控分支：补挑战头 + 重算 DS（新时间戳）再发一次
    retry_headers = dict(headers)
    retry_headers.update(CHALLENGE_META.get(game_id, CHALLENGE_META["genshin"]))
    retry_headers["DS"] = build_ds(query=params)
    logger.debug("[%s] 重试请求头（脱敏）：%s", game_id, {
        k: ("***" if k == "Cookie" else v) for k, v in retry_headers.items()
    })
    return _daily_note_once(game_id, url, params, retry_headers)


#: getGameRecordCard 的 game_id -> 本项目 game_id
#: （实测记录见 RETCODE_HINTS 上方注释：该接口在本项目环境 retcode=0 通过）
RECORD_CARD_GAME_IDS: Dict[int, str] = {2: "genshin", 6: "starrail", 8: "zzz"}

#: 米游社「我的角色」列表接口：一次请求返回该账号在**所有游戏**里的角色，
#: 扫码登录后用它回填各游戏的 uid/server/nickname，并识别"账号没玩过这款游戏"。
RECORD_CARD_PATH = "/game_record/card/wapi/getGameRecordCard"


def fetch_game_roles(
    ltuid: str,
    ltoken: str,
    cookie: str = "",
    base_url: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """拉取该米游社账号的游戏角色列表（getGameRecordCard，GET，只读）。

    Args:
        cookie: 完整 Cookie 串（扫码后拿到的那串），空则用 ltuid+ltoken 拼。

    Returns:
        {game_id: {"uid": "123456789", "server": "cn_gf01",
                   "nickname": "旅行者", "level": 60}} —— 只含认得出的游戏。
        某款游戏不在结果里 = 该账号没玩过它。

    Raises:
        MiyousheQueryError: 网络 / HTTP / retcode != 0。扫码流程应当**自行降级**
        （拉不到列表就跳过回填，保持旧行为），绝不能因为这一步失败让登录报错。
    """
    base_url = (base_url or RECORD_BASE).rstrip("/")
    params: Dict[str, Any] = {"uid": ltuid}
    headers = build_headers(
        ltuid, ltoken, query=params, cookie=cookie, with_device=False,
    )
    logger.debug(
        "[roles] 请求 %s?uid=%s（代理=%s）", RECORD_CARD_PATH, ltuid,
        _mask_proxy(_PROXY) if _PROXY else "直连",
    )
    data = _daily_note_once("roles", base_url + RECORD_CARD_PATH, params, headers)

    roles: Dict[str, Dict[str, Any]] = {}
    raw_list = data.get("list")
    if not isinstance(raw_list, list):
        # 没玩过任何游戏的账号 list 可能为空数组；结构异常才报错
        if raw_list == []:
            return roles
        raise MiyousheQueryError(f"角色列表返回缺少 list 数组：{type(raw_list).__name__}")
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            game_id = RECORD_CARD_GAME_IDS.get(int(item.get("game_id")))
        except (TypeError, ValueError):
            continue
        if not game_id:
            continue
        # getGameRecordCard 的字段名是 game_role_id（role_id 是另一接口的参数名，
        # 写错会让每条记录 uid 取空被跳过 → 全部误判「未创建角色」）
        uid = str(item.get("game_role_id") or "").strip()
        if not uid:
            continue
        try:
            level = int(item.get("level"))
        except (TypeError, ValueError):
            level = None
        roles[game_id] = {
            "uid": uid,
            "server": str(item.get("region") or "").strip(),
            "nickname": str(item.get("nickname") or "").strip(),
            "level": level,
        }
    return roles


def require_field(data: Dict[str, Any], *path: str) -> Any:
    """按路径取字段，任何一级缺失/为 None 都报字段缺失错误（需求 6）。"""
    node: Any = data
    walked: list = []
    for key in path:
        walked.append(key)
        if not isinstance(node, dict) or key not in node or node[key] is None:
            raise MiyousheQueryError("米游社返回的数据不完整，可能需要更新软件版本")
        node = node[key]
    return node


def require_int(data: Dict[str, Any], *path: str) -> int:
    """取字段并转 int，转换失败同样按字段异常处理。"""
    value = require_field(data, *path)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MiyousheQueryError("米游社返回的数据格式有变化，可能需要更新软件版本") from exc
