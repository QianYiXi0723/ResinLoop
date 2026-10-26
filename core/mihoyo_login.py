"""米游社（米哈游通行证）扫码登录：手机米游社 App 扫码 -> 自动拿到 Cookie + 绑定设备。

协议来源（逐字核对，非猜测）：
    - UIGF 米游社扫码登录文档：
      https://uigf.org/zh/mihoyo-api-collection/hoyolab/login/qrcode_hoyolab
    - AUTO-MAS app/tools/miyoushe_qr.py（2026 现行实现，Web/App 双通道）

流程：
    1. POST passport-api.miyoushe.com/account/ma-cn-passport/web/createQRLogin
       必带请求头 x-rpc-app_id: bll8iq97cem8、x-rpc-device_id
       -> data: {url, ticket}
    2. 手机米游社 App 扫 data.url 的二维码并确认
    3. POST .../web/queryQRLoginStatus  {"ticket": ...} 轮询
       status: Created(未扫) / Scanned(已扫) / Confirmed(已确认)
       retcode -3501 二维码过期、-3505 用户取消
    4. Confirmed 后从该响应的 Set-Cookie 里取
       ltuid/account_id、ltoken、cookie_token、stoken、mid 等；
       若只给了 stoken_v2 没给 ltoken，用 getLTokenBySToken 派生 v1 ltoken。

设备绑定（这是解决 5003 风控的关键一步）：
    扫码用的 x-rpc-device_id 会原样写进 logs/device_fp.json 的三款游戏条目，
    并为这个 device_id 重新签发配套 device_fp（getFp 接口）。
    之后 dailyNote 请求用的设备 ID / 指纹就和"扫码登录那台设备"完全一致，
    而不是每次随机一套合成指纹。
"""

from __future__ import annotations

import json
import logging
import time
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Optional, Tuple

from .hypergryph_login import render_qr
from .mihoyo_api import (
    APP_VERSION,
    USER_AGENT,
    _import_requests,
    _proxies,
    bind_device,
    generate_fake_fp,
)

logger = logging.getLogger("dailycheck.mihoyologin")

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CREATE_QR_URL = "https://passport-api.miyoushe.com/account/ma-cn-passport/web/createQRLogin"
CHECK_QR_URL = "https://passport-api.miyoushe.com/account/ma-cn-passport/web/queryQRLoginStatus"

#: 米游社 App 的 app_id（社区通行常量）
APP_ID = "bll8iq97cem8"

#: 扫码请求头（对齐 AUTO-MAS / genshin.py 的 QRCODE_HEADERS）。
#: 注意：x-rpc-device_fp **不写死**——社区里流传的 38d7fa104e5d7 是被成千上万人
#: 用过的公开常量，拿它登录等于自报"脚本"，正是账号被标记的高危因素之一。
#: 这里改为按本次 device_id 现场签发一个与查询设备同源的真机指纹（见 _qr_headers）。
QR_HEADERS: Dict[str, str] = {
    "x-rpc-app_id": APP_ID,
    "x-rpc-client_type": "4",
    "x-rpc-game_biz": "bbs_cn",
    "User-Agent": USER_AGENT,
}

#: 扫码阶段设备指纹缓存（同一 device_id 只签一次，避免每次轮询都打 getFp）
_QR_FP_CACHE: Dict[str, str] = {}


def _qr_headers(device_id: str) -> Dict[str, str]:
    """扫码请求头：补上本次设备的 device_id 与现场签发的 device_fp。"""
    fp = _QR_FP_CACHE.get(device_id)
    if not fp:
        try:
            fp = generate_fake_fp(device_id)
        except Exception as exc:  # getFp 挂了也不该阻断登录
            logger.warning("扫码设备指纹签发失败（%s），降级为本地随机指纹", exc)
            fp = ""
        _QR_FP_CACHE[device_id] = fp
    headers = dict(QR_HEADERS, **{"x-rpc-device_id": device_id})
    if fp:
        headers["x-rpc-device_fp"] = fp
    return headers

#: ltoken 派生接口（stoken_v2 + mid -> ltoken v1）
LTOKEN_URL = "https://passport-api.mihoyo.com/account/auth/api/getLTokenBySToken"

#: 轮询间隔 / 默认超时（秒）
POLL_INTERVAL = 2
DEFAULT_TIMEOUT = 180

#: 扫码状态
STATUS_WAITING = "Created"
STATUS_SCANNED = "Scanned"
STATUS_CONFIRMED = "Confirmed"


class MiyousheLoginError(Exception):
    """扫码登录任一步失败（网络 / retcode / 超时 / 取消 / 字段缺失）。"""


# --------------------------------------------------------------------------- #
# 步骤 1：创建二维码
# --------------------------------------------------------------------------- #


def create_qr(device_id: str) -> Dict[str, str]:
    """创建扫码会话，返回 {"ticket", "url"}。"""
    requests = _import_requests()
    headers = _qr_headers(device_id)
    try:
        resp = requests.post(
            CREATE_QR_URL, headers=headers, proxies=_proxies(), timeout=10
        )
    except Exception as exc:
        raise MiyousheLoginError(f"创建二维码网络异常：{exc}") from exc
    if resp.status_code != 200:
        raise MiyousheLoginError(f"创建二维码 HTTP {resp.status_code}：{resp.text[:120]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MiyousheLoginError(f"创建二维码返回非 JSON：{resp.text[:120]}") from exc

    retcode = payload.get("retcode")
    if retcode != 0:
        raise MiyousheLoginError(
            f"创建二维码失败：retcode={retcode}，message={payload.get('message')}"
            "（-3001 表示请求头缺少 x-rpc-app_id / x-rpc-device_id）"
        )
    data = payload.get("data") or {}
    url = str(data.get("url") or "")
    ticket = str(data.get("ticket") or "")
    if not (url and ticket):
        raise MiyousheLoginError(f"创建二维码返回缺少 url/ticket：{str(data)[:120]}")
    return {"url": url, "ticket": ticket}


# --------------------------------------------------------------------------- #
# 步骤 2/3：轮询 + 解析 Set-Cookie
# --------------------------------------------------------------------------- #


def _parse_set_cookie_headers(raw_headers: List[str]) -> Dict[str, str]:
    """把多条 Set-Cookie 解析成 {name: value}（同名后者覆盖前者）。"""
    jar: Dict[str, str] = {}
    for line in raw_headers:
        if not line:
            continue
        first = line.split(";", 1)[0]
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            jar[name] = value
    return jar


def _collect_cookies(resp: Any, data: Dict[str, Any]) -> Dict[str, str]:
    """优先从 Set-Cookie 响应头收集，兜底兼容响应体里的 cookies 字段。"""
    raw_list: List[str] = []
    try:
        raw_list = list(resp.raw.headers.getlist("Set-Cookie"))
    except Exception:
        merged = resp.headers.get("Set-Cookie") or ""
        if merged:
            raw_list = [seg for seg in merged.split(",") if "=" in seg]

    jar = _parse_set_cookie_headers(raw_list)

    # 兜底：有些返回把 cookie 放在 data.cookies（字符串或字典）
    body_cookies = data.get("cookies")
    if isinstance(body_cookies, str) and body_cookies:
        for part in body_cookies.split(";"):
            if "=" in part:
                name, _, value = part.partition("=")
                jar.setdefault(name.strip(), value.strip())
    elif isinstance(body_cookies, dict):
        for name, value in body_cookies.items():
            jar.setdefault(str(name), str(value))
    return jar


#: v2 Cookie 名 -> v1 别名（dailyNote 认 v1 名）
_COOKIE_ALIASES = {
    "ltuid_v2": "ltuid",
    "stuid": "ltuid",
    "stuid_v2": "ltuid",
    "login_uid": "ltuid",
    "account_id_v2": "account_id",
    "ltoken_v2": "ltoken",
    "cookie_token_v2": "cookie_token",
    "stoken_v2": "stoken",
    "mid_v2": "mid",
    "ltmid_v2": "mid",
    "account_mid_v2": "mid",
}


def _normalize_cookies(jar: Dict[str, str]) -> Dict[str, str]:
    """补齐 v1 别名（不删除原始字段），返回新字典。"""
    result = dict(jar)
    for source, target in _COOKIE_ALIASES.items():
        if jar.get(source) and not result.get(target):
            result[target] = jar[source]
    return result


def _fetch_ltoken_by_stoken(stoken: str, mid: str, uid: str) -> str:
    """stoken + mid -> ltoken v1（仅在 Set-Cookie 未直接下发 ltoken 时调用）。"""
    requests = _import_requests()
    headers = {
        "x-rpc-client_type": "1",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-app_version": APP_VERSION,
        "User-Agent": "Hyperion/275 CFNetwork/1402.0.8 Darwin/22.2.0",
    }
    cookies = {"stoken": stoken, "mid": mid, "stuid": uid}
    try:
        resp = requests.get(
            LTOKEN_URL, headers=headers, cookies=cookies, proxies=_proxies(), timeout=10
        )
        payload = resp.json()
    except Exception as exc:
        logger.warning("派生 ltoken 失败（忽略）：%s", exc)
        return ""
    if payload.get("retcode") != 0:
        logger.warning("派生 ltoken 返回错误：%s", payload.get("message"))
        return ""
    token = str((payload.get("data") or {}).get("ltoken") or "")
    return token


def poll_confirm(device_id: str, ticket: str, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """轮询扫码状态，确认后返回 {"cookies": {...}, "raw_fields": [...]}。"""
    requests = _import_requests()
    headers = _qr_headers(device_id)
    start = time.time()
    fail_count = 0

    while time.time() - start < timeout:
        try:
            resp = requests.post(
                CHECK_QR_URL,
                headers=headers,
                json={"ticket": ticket},
                proxies=_proxies(),
                timeout=10,
            )
            fail_count = 0
        except Exception as exc:
            fail_count += 1
            if fail_count >= 5:
                raise MiyousheLoginError(f"轮询扫码状态连续失败：{exc}") from exc
            time.sleep(POLL_INTERVAL)
            continue

        try:
            payload = resp.json()
        except ValueError:
            time.sleep(POLL_INTERVAL)
            continue

        retcode = payload.get("retcode")
        data = payload.get("data") or {}
        if retcode != 0:
            message = str(payload.get("message") or "")
            if retcode == -3501:
                raise MiyousheLoginError("二维码已过期，请重新运行登录")
            if retcode == -3505:
                raise MiyousheLoginError("已在手机上取消登录")
            raise MiyousheLoginError(f"查询扫码状态失败：retcode={retcode}，{message}")

        status = data.get("status")
        if status == STATUS_CONFIRMED:
            jar = _normalize_cookies(_collect_cookies(resp, data))
            return {"cookies": jar, "raw_fields": sorted(jar)}
        if status in ("Expired", "Canceled"):
            raise MiyousheLoginError("二维码已过期或被取消，请重新运行登录")
        # Created / Scanned 继续等
        time.sleep(POLL_INTERVAL)

    raise MiyousheLoginError(f"等待扫码超时（{timeout} 秒）")


# --------------------------------------------------------------------------- #
# 步骤 4：整理成可写配置的 Cookie 串
# --------------------------------------------------------------------------- #


def build_cookie_string(jar: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
    """从 Cookie 字典整理出 dailyNote 需要的 Cookie 串。

    Returns:
        (cookie_string, {"ltuid": ..., "ltoken": ..., ...})

    Raises:
        MiyousheLoginError: 缺少 ltuid 或 ltoken（且无法用 stoken 派生）。
    """
    uid = str(jar.get("ltuid") or jar.get("account_id") or "").strip()
    ltoken = str(jar.get("ltoken") or "").strip()
    stoken = str(jar.get("stoken") or "").strip()
    mid = str(jar.get("mid") or "").strip()

    if not ltoken and stoken and mid and uid:
        logger.info("Set-Cookie 未直接给 ltoken，改用 stoken+mid 派生")
        ltoken = _fetch_ltoken_by_stoken(stoken, mid, uid)

    if not uid:
        raise MiyousheLoginError("扫码成功但未拿到账号 ID（ltuid/account_id）")
    if not ltoken:
        raise MiyousheLoginError(
            "扫码成功但未拿到 ltoken（且 stoken 派生失败）。"
            f"返回字段：{', '.join(sorted(jar)) or '空'}"
        )

    parts = [f"ltuid={uid}", f"ltoken={ltoken}"]
    cookie_token = str(jar.get("cookie_token") or "").strip()
    if cookie_token:
        parts.append(f"cookie_token={cookie_token}")
    if mid:
        parts.append(f"mid={mid}")
    parts.append(f"account_id={uid}")
    # v2 字段一并保留：新版 passport 下发的多是 *_v2，部分接口按 v2 名读取；
    # 与 v1 同值时去重，避免 Cookie 头里出现完全重复的键值。
    for name in ("ltuid_v2", "ltoken_v2", "cookie_token_v2", "account_id_v2", "mid_v2"):
        value = str(jar.get(name) or "").strip()
        if value and all(f"{name}={value}" != part for part in parts):
            parts.append(f"{name}={value}")

    return "; ".join(parts), {"ltuid": uid, "ltoken": ltoken, "mid": mid}


# --------------------------------------------------------------------------- #
# 一键流程
# --------------------------------------------------------------------------- #


def run_qr_login(timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """完整扫码登录：生成二维码 -> 扫码 -> 拿 Cookie -> 绑定设备。

    Returns:
        {"cookie": str, "ltuid": str, "ltoken": str, "device_id": str,
         "device_fps": {game: fp}}

    Raises:
        MiyousheLoginError: 任一步失败。
    """
    import uuid

    device_id = str(uuid.uuid4()).lower()  # 与 GsCore / dailyNote 请求头保持同一格式
    session = create_qr(device_id)
    print(f"[1/4] 二维码会话已创建（设备 ID：{device_id[:8]}…）")
    render_qr(session["url"])
    print("请用手机【米游社 App】扫描上方二维码，并在手机上确认登录...")

    print("[2/4] 等待扫码确认...")
    result = poll_confirm(device_id, session["ticket"], timeout=timeout)
    print(f"      已确认，收到 Cookie 字段：{', '.join(result['raw_fields'])}")

    print("[3/4] 整理登录态...")
    cookie, creds = build_cookie_string(result["cookies"])
    print(f"      ltuid={creds['ltuid']}，ltoken 已获取（{creds['ltoken'][:8]}…）")

    print("[4/4] 绑定设备指纹（三款游戏统一用本次扫码设备）...")
    device_fps = bind_device(device_id)
    print("      设备已绑定：" + "、".join(f"{k}={v[:8]}…" for k, v in device_fps.items()))

    return {
        "cookie": cookie,
        "ltuid": creds["ltuid"],
        "ltoken": creds["ltoken"],
        "device_id": device_id,
        "device_fps": device_fps,
    }
