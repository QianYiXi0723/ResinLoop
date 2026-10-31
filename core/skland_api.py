"""森空岛（Skland）只读查询层。

抄写来源：starudream/sign-task 的 pkg/skland（Go），算法逐字对应翻译：
    请求头：platform / timestamp / dId / vName / cred / sign
    签名：  content = path + (GET: 排序 query | POST: 紧凑 JSON body)
                   + timestamp + json(platform/timestamp/dId/vName)
            sign = md5( hex( HMAC-SHA256(token, content) ) )
    dId 是社区通用硬编码常量（非个人设备标识），VName 跟随 App 版本。

本层只做三件事：
    1. 用鹰角通行证 token 换取森空岛 cred/token（自动缓存，失效自动重换）
    2. 查询绑定角色（binding，同时验证凭证有效性）
    3. 查询每日签到状态（attendance，只读，绝不代签）

不做任何写操作：签到接口（POST /attendance）在代码里不存在。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode as _urlencode

logger = logging.getLogger("dailycheck.skland")

# --------------------------------------------------------------------------- #
# 常量（抄自 sign-task pkg/skland/api/preset.go）
# --------------------------------------------------------------------------- #

ADDR_HYPERGRYPH = "https://as.hypergryph.com"
ADDR_ZONAI = "https://zonai.skland.com"

USER_AGENT = "Skland/1.5.1 (com.hypergryph.skland; build:100501001; Android 33; ) Okhttp/4.11.0"

#: 森空岛 AppCode（社区通用常量）
APP_CODE_SKLAND = "4ca99fa6b56cc2ba"
#: 硬编码设备 ID（sign-task 同款静态常量，非个人隐私标识）
D_ID = "743a446c83032899"
PLATFORM = "1"
V_NAME = "1.5.1"

#: gameId：明日方舟=1、终末地=3
GAME_ID_ARKNIGHTS = "1"
GAME_ID_ENDFIELD = "3"

#: 单次请求超时（秒）
REQUEST_TIMEOUT = 10

#: skland 换票接口 `/user/oauth2/v2/grant` 对 token 的硬性长度上限。
#: 2026-10-26 用真实接口逐字符实测：128 通过（进入"登录已过期"校验），
#: 129 起返回 `GrantV2Req.token ... failed on the 'max' tag`。
#: 因此登录拿到 176 字符的"长 token"必然失败——正确 token 是登录短 token（40~100 字符）。
MAX_TOKEN_LEN = 128

#: cred/token 本地缓存目录（鹰角 token -> 森空岛 cred+token），避免每次重换。
#: 存 logs/ 下（已被 .gitignore 排除）。
#:
#: 【多账号隔离】明日方舟和终末地可能在不同的鹰角通行证账号下，凭证缓存
#: 必须按游戏分文件（skland_cred_arknights.json / skland_cred_endfield.json），
#: 由 _cred_cache_path() 生成，绝不能共用一个文件互相覆盖。
_CRED_CACHE_DIR = Path(__file__).resolve().parent.parent / "logs"

#: 允许按游戏分缓存的白名单；未知游戏落到 default（正常不会发生）
_CRED_CACHE_GAMES = ("arknights", "endfield")

#: 森空岛 cred 是**短期凭证**：实测换取后数小时即失效，接口返回
#: HTTP 401 / {"code":10000,"message":"请求异常"}（与签名错误的表现一致）。
#: 缓存超过该时长就主动重新换取，避免每轮先失败一次。
CRED_TTL_SECONDS = 4 * 3600


def _cred_cache_path(game: str) -> Path:
    safe_game = game if game in _CRED_CACHE_GAMES else "default"
    return _CRED_CACHE_DIR / f"skland_cred_{safe_game}.json"


class SklandQueryError(Exception):
    """森空岛查询失败（网络 / HTTP / 业务错误码 / 字段缺失），由 main 捕获标记为查询失败。"""


class SklandAuthError(SklandQueryError):
    """森空岛凭证缺失 / 换取失败。"""

    #: 凭证失效类别随异常类自带——森空岛错误文案没有 retcode= 字样，
    #: main._query_account 的正则兜底对它永远无效，必须走类属性
    cred_kind = "auth"


class GameNotBoundError(SklandQueryError):
    """凭证有效，但这个森空岛账号下**没有绑这款游戏的角色**。

    典型场景：勾选了明日方舟并扫码，但该鹰角通行证从来没玩过方舟 ——
    登录本身是成功的，查不到数据只是因为没角色。它不该显示成红色
    「查询失败」，也不该触发凭证告警（凭证是好的），所以单独一档，
    玩家在游戏里建号后会自动生效。
    """


# --------------------------------------------------------------------------- #
# 凭证：鹰角通行证 token -> 森空岛 cred + token（自动缓存）
# --------------------------------------------------------------------------- #


def _load_cred_cache(path: Path) -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


#: 凭证缓存读改写闸：并行查询同游戏多账号时，无锁整份覆盖会丢掉对方刚写入的
#: 条目（写入方各自持着旧快照）；网络换取在锁外，锁只包「重读→更新→原子写」。
_CRED_CACHE_LOCK = threading.Lock()


def _save_cred_cache(path: Path, cache: Dict[str, Dict[str, str]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cache, ensure_ascii=False, indent=2))
        os.replace(tmp_name, path)
    except OSError as exc:
        logger.warning("森空岛凭证缓存写入失败（不影响本次运行）：%s", exc)


def _import_requests():
    try:
        import requests  # noqa: PLC0415 - 故意延迟导入
    except ImportError as exc:
        raise SklandQueryError("缺少依赖 requests，请先执行：pip install -r requirements.txt") from exc
    return requests


def _grant_code(ark_token: str) -> str:
    """第 1 步：鹰角登录 token -> oauth2 授权 code（带格式预检）。"""
    # 防格式污染：token 里混入空格/换行会被服务端当作非法 base64 或超长字符串
    token = (ark_token or "").strip()
    if not token:
        raise SklandAuthError("鹰角 token 为空，请重新扫码登录")
    if "请填写" in token:
        # 出厂模板占位符（"请填写鹰角通行证Token"）：不拦会进 Authorization 头，
        # 'latin-1' 编码爆炸，用户看到的是天书而不是"还没绑定"
        raise SklandAuthError(
            "鹰角通行证 token 还没绑定（仍是出厂占位符），"
            "请在「账号绑定」页勾选后扫码登录森空岛"
        )
    if any(ch.isspace() for ch in token):
        raise SklandAuthError(
            f"鹰角 token 中间含空白字符（长度 {len(token)}），请重新扫码登录"
        )
    if len(token) > MAX_TOKEN_LEN:
        raise SklandAuthError(
            f"鹰角 token 长度 {len(token)} 超过接口上限 {MAX_TOKEN_LEN}："
            "这通常是旧版扫码流程换出来的\"长 token\"，请重新运行 "
            "python main.py --login-skland <arknights|endfield> 获取登录短 token"
        )

    requests = _import_requests()
    body = {"type": 0, "token": token, "appCode": APP_CODE_SKLAND}
    logger.debug("[skland] grant 请求 token 长度=%d，前缀=%r", len(token), token[:8])
    try:
        resp = requests.post(
            ADDR_HYPERGRYPH + "/user/oauth2/v2/grant",
            json=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        grant = resp.json()
    except Exception as exc:
        raise SklandQueryError(f"森空岛换取凭证时网络异常：{exc}") from exc

    logger.debug("[skland] grant 原始返回：%s", str(grant)[:500])
    if grant.get("status") != 0:
        msg = grant.get("msg") or grant.get("message") or ""
        raise SklandAuthError(
            f"鹰角 token 换取授权失败：status={grant.get('status')}，msg={msg}"
            f"（token 长度={len(token)}；正确 token 是登录短 token，40~100 字符、不以 eyJ 开头）"
        )
    code = (grant.get("data") or {}).get("code") or ""
    if not code:
        raise SklandAuthError("鹰角授权返回缺少 code 字段")
    return code


def verify_ark_token(ark_token: str) -> Dict[str, Any]:
    """预检：token 能否通过 skland 的 grant 校验。

    Returns:
        {"ok": True} 或 {"ok": False, "error": 原因}
    """
    try:
        _grant_code(ark_token)
    except SklandQueryError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _exchange_cred(ark_token: str) -> Dict[str, str]:
    """用鹰角登录 token 走官方流程换取森空岛 cred + token。

    流程（抄自 sign-task pkg/skland/job/auth.go）：
        POST as.hypergryph.com/user/oauth2/v2/grant              {type:0, token, appCode}
        -> {uid, code}
        POST zonai.skland.com/api/v1/user/auth/generate_cred_by_code  {kind:1, code}
        -> {userId, cred, token}
    """
    code = _grant_code(ark_token)

    requests = _import_requests()
    try:
        resp2 = requests.post(
            ADDR_ZONAI + "/api/v1/user/auth/generate_cred_by_code",
            json={"kind": 1, "code": code},
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        gen = resp2.json()
    except Exception as exc:
        raise SklandQueryError(f"森空岛换取凭证时网络异常：{exc}") from exc

    logger.debug("[skland] generate_cred_by_code 原始返回：%s", str(gen)[:500])
    if gen.get("code") != 0:
        raise SklandAuthError(
            f"换取森空岛 cred 失败：code={gen.get('code')}，message={gen.get('message')}"
        )
    data = gen.get("data") or {}
    cred = str(data.get("cred") or "").strip()
    token = str(data.get("token") or "").strip()
    if not (cred and token):
        raise SklandAuthError("generate_cred_by_code 返回缺少 cred/token 字段")
    return {"cred": cred, "token": token}


def _cred_age_seconds(entry: Dict[str, Any]) -> Optional[float]:
    """缓存条目的年龄（秒）。旧版缓存没有 saved_at 时返回 None（新鲜度未知）。"""
    try:
        saved_at = float(entry.get("saved_at") or 0)
    except (TypeError, ValueError):
        return None
    if saved_at <= 0:
        return None
    return max(0.0, time.time() - saved_at)


def is_cred_expired_error(exc: BaseException) -> bool:
    """判断异常是否属于"凭证过期/失效"。用于触发重新换取后重试一次。

    森空岛对过期 cred 的返回：HTTP 401 + {"code":10000,"message":"请求异常"}。
    签名错误（token 不对）表现完全相同，所以重换一次也能顺带修复 token 变更。
    """
    text = str(exc)
    return (
        "HTTP 401" in text
        or "code=10000" in text
        or '"code":10000' in text
        or "请求异常" in text
    )


def get_skland_credentials(
    credential: Dict[str, Any], game: str = "", force_refresh: bool = False
) -> Dict[str, str]:
    """从账号 credential 里取森空岛 cred/token。

    Args:
        credential: 账号的 credential 字典。
        game: 游戏标识（arknights / endfield），决定用哪个缓存文件。
              【重要】两款游戏可能在不同的鹰角通行证账号下，必须分开缓存，
              传错 game 会导致用 A 账号的缓存去跑 B 账号。
        force_refresh: 忽略缓存，强制重新换取（401 兜底重试用）。

    三种填法（优先级从高到低）：
        1. 直接给 {"cred": "...", "token": "..."}（token 为森空岛签名 token）
        2. 只给 {"cred": "..."} —— 缺签名 token，无法签名，报错提示补 token
        3. 给 {"token": "<鹰角通行证 Token>"} —— 自动换取 cred+token 并缓存
    """
    direct_cred = str(credential.get("cred", "") or "").strip()
    direct_token = str(credential.get("token", "") or "").strip()
    if direct_cred and direct_token:
        return {"cred": direct_cred, "token": direct_token}

    cache_path = _cred_cache_path(game)
    cache = _load_cred_cache(cache_path)
    entry = cache.get(direct_token) if direct_token else None
    if entry and force_refresh:
        logger.info("强制重新换取森空岛凭证（%s）", cache_path.name)
        entry = None
    elif entry:
        age = _cred_age_seconds(entry)
        if age is not None and age > CRED_TTL_SECONDS:
            logger.info(
                "森空岛凭证缓存已超过 TTL（%.1f 小时前换取，上限 %.0f 小时），重新换取",
                age / 3600,
                CRED_TTL_SECONDS / 3600,
            )
            entry = None
        elif age is None:
            logger.debug("森空岛凭证缓存缺少 saved_at，先按可用处理（401 时会自动重换）")
    if entry:
        return {"cred": str(entry.get("cred") or ""), "token": str(entry.get("token") or "")}

    if direct_cred and not direct_token:
        raise SklandAuthError(
            "credential 里给了 cred 但缺 token：森空岛请求签名需要 token 作为密钥，"
            "请补齐 token（鹰角通行证 Token 即可，脚本会自动换取）"
        )
    if direct_token:
        result = _exchange_cred(direct_token)   # 网络换取在锁外，不阻塞别的 key
        with _CRED_CACHE_LOCK:
            # 锁内重读：等网络期间其他线程可能已写盘，直接用进锁前的旧快照会丢条目
            cache = _load_cred_cache(cache_path)
            cache[direct_token] = {**result, "saved_at": int(time.time())}
            _save_cred_cache(cache_path, cache)
        logger.info("已用鹰角 token 换取并缓存森空岛凭证（%s）", cache_path.name)
        return result

    raise SklandAuthError(
        "森空岛还没有登录过：请先在「账号绑定」页扫码登录森空岛，"
        "获取方法见 README「森空岛接入」一节"
    )


# --------------------------------------------------------------------------- #
# 请求签名（抄自 sign-task pkg/skland/api/sign.go，逐字翻译）
# --------------------------------------------------------------------------- #


def _sign_headers(timestamp: str) -> Dict[str, str]:
    """参与签名与实际发送的业务头（顺序固定，与 Go struct 对齐）。"""
    return {"platform": PLATFORM, "timestamp": timestamp, "dId": D_ID, "vName": V_NAME}


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _hmac256_hex(key: str, content: str) -> str:
    return hmac.new(key.encode(), content.encode(), hashlib.sha256).hexdigest()


def skland_sign(
    token: str,
    method: str,
    path: str,
    query: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """构造森空岛请求的全部认证头（platform/timestamp/dId/vName/cred 由调用方补 cred）。

    返回除 cred 之外的头；cred 由调用方以独立头发送（与 sign-task 一致）。
    """
    timestamp = str(int(time.time()))
    headers = _sign_headers(timestamp)

    if method.upper() == "GET":
        # Go url.Values.Encode()：按 key 字典序排序后 URL 编码
        str_part = _urlencode(sorted((query or {}).items()))
        body_json = ""
    else:
        # Go json.Marshal(map)：按 key 字典序、紧凑分隔符
        str_part = ""
        body_json = json.dumps(body or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    content = path + str_part + timestamp + json.dumps(headers, ensure_ascii=False, separators=(",", ":"))
    signature = _md5_hex(_hmac256_hex(token, content))

    return {
        "platform": headers["platform"],
        "timestamp": timestamp,
        "dId": headers["dId"],
        "vName": headers["vName"],
        "sign": signature,
    }


# --------------------------------------------------------------------------- #
# 接口：绑定角色 + 签到状态（全部只读）
# --------------------------------------------------------------------------- #


def _check_payload(payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    """hypergryph 风格 {status,msg} 与 skland 风格 {code,message} 统一校验。"""
    status = payload.get("status")
    code = payload.get("code")
    ok = (status is None or status == 0) and (code is None or code == 0)
    if not ok:
        msg = payload.get("msg") or payload.get("message") or ""
        ret = status if status is not None else code
        raise SklandQueryError(f"接口 {source} 返回错误码 {ret}：{msg}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SklandQueryError(f"接口 {source} 返回缺少 data 字典")
    return data


def request_skland(
    method: str,
    path: str,
    cred: str,
    token: str,
    query: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """调用森空岛接口，校验业务码后返回 data 字典。

    Args:
        extra_headers: 额外业务头（如终末地必需的 sk-game-role），
            不参与签名（与 sign-task 一致：签名只覆盖 platform/timestamp/dId/vName）。

    Raises:
        SklandQueryError: 网络异常 / HTTP 非 200 / 业务错误码 != 0 / 缺 data。
    """
    requests = _import_requests()
    url = ADDR_ZONAI + path
    headers = skland_sign(token, method, path, query=query, body=body)
    headers["cred"] = cred
    headers["User-Agent"] = USER_AGENT
    headers["Accept-Encoding"] = "gzip"
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        headers["Content-Type"] = "application/json"

    logger.debug("[skland] %s %s%s", method, path, ("?" + _urlencode(sorted((query or {}).items()))) if query else "")

    try:
        response = requests.get(
            url, params=query, headers=headers, timeout=REQUEST_TIMEOUT
        ) if method.upper() == "GET" else requests.post(
            url, json=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except Exception as exc:
        raise SklandQueryError(f"网络请求失败：{exc}") from exc

    if response.status_code != 200:
        logger.debug("[skland] HTTP %s 原始响应：%s", response.status_code, response.text[:500])
        raise SklandQueryError(f"森空岛接口暂时没有响应（HTTP {response.status_code}），请稍后再试")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.debug("[skland] 非 JSON 原始响应：%s", response.text[:500])
        raise SklandQueryError("森空岛返回了无法解析的数据，请稍后再试") from exc

    logger.debug("[skland] %s 原始返回（脱敏前按需看 --debug 日志）：%s", path, str(payload)[:200])
    return _check_payload(payload, path)


def list_players(cred: str, token: str) -> Dict[str, Any]:
    """查询绑定角色列表：GET /api/v1/game/player/binding。"""
    return request_skland("GET", "/api/v1/game/player/binding", cred, token)


def get_attendance_status(
    cred: str,
    token: str,
    game_id: str,
    uid: str,
    role_id: str = "",
    server_id: str = "",
) -> Dict[str, Any]:
    """查询某游戏今日签到状态（只读，不触发签到）。

    注意：主流程已改为查询"游戏内每日任务"，本函数仅保留供签到类需求备用。

    - 明日方舟：GET /api/v1/game/attendance?gameId=1&uid=...
    - 终末地：  GET /api/v1/game/endfield/attendance?gameId=3&roleId=...&serverId=...
                （必须带 sk-game-role: 3_roleId_serverId）
    """
    if game_id == GAME_ID_ENDFIELD:
        path = "/api/v1/game/endfield/attendance"
        query = {"gameId": game_id, "roleId": role_id, "serverId": server_id}
        extra = {"sk-game-role": f"{game_id}_{role_id}_{server_id}"}
    else:
        path = "/api/v1/game/attendance"
        query = {"gameId": game_id, "uid": uid}
        extra = None
    return request_skland("GET", path, cred, token, query=query, extra_headers=extra)


# --------------------------------------------------------------------------- #
# 游戏内数据：每日任务进度（主流程实际使用的接口）
# --------------------------------------------------------------------------- #


def get_arknights_player_info(cred: str, token: str, uid: str) -> Dict[str, Any]:
    """明日方舟玩家全量数据：GET /api/v1/game/player/info?uid=<游戏 uid>。

    返回体里 ``data.routine.daily = {current, total}`` 就是游戏内每日任务进度
    （社区文档 ProbiusOfficial/Skland_API 的 API 数据解读，与 dump.json 实测一致：
    routine.daily={"current":0,"total":10}、routine.weekly={"current":0,"total":13}）。
    """
    data = request_skland("GET", "/api/v1/game/player/info", cred, token, query={"uid": uid})
    logger.debug("[skland] player/info 顶层字段：%s", list(data.keys())[:30])
    return data


def get_endfield_card_detail(cred: str, token: str, role_id: str, server_id: str) -> Dict[str, Any]:
    """终末地角色/存档详情：GET /api/v1/game/endfield/card/detail。

    必须带 ``sk-game-role: 3_<roleId>_<serverId>``。返回 ``data.detail``，
    其中 ``dailyMission = {dailyActivation, maxDailyActivation}`` 即每日活跃度
    （实测 2026-10-26：100/100；weeklyMission={score,total} 为每周任务）。
    """
    data = request_skland(
        "GET",
        "/api/v1/game/endfield/card/detail",
        cred,
        token,
        extra_headers={"sk-game-role": f"{GAME_ID_ENDFIELD}_{role_id}_{server_id}"},
    )
    detail = data.get("detail")
    if not isinstance(detail, dict):
        raise SklandQueryError("终末地 card/detail 返回缺少 detail 字典")
    logger.debug("[skland] card/detail 顶层字段：%s", list(detail.keys())[:30])
    return detail


def today_signed(data: Dict[str, Any]) -> Optional[bool]:
    """从 attendance 返回里判断"今天是否已签"。

    判定逻辑与社区实现 skland-daily-attendance/utils/attendance/shared.ts 对齐：
        1. 返回体里有 records 数组（明日方舟）：按"上海时区日期"比较，
           任一记录的日期等于今天 -> 已签；一条都不匹配 -> 未签（空数组=从没签过，也是未签）。
        2. 没有 records（终末地）：直接看 hasToday（boolean，服务端权威判定）。
        3. 两者都没有 -> None（结构变化，交调用方报字段缺失，绝不瞎猜）。

    注意：绝不能拿 calendar 里的 done 兜底——那是 31 天签到日历，任何一个 done
    都会让整月都判成"已签"。
    """
    records = data.get("records")
    logger.debug(
        "[skland] 签到判定输入：records=%s，hasToday=%s",
        len(records) if isinstance(records, list) else "缺失",
        data.get("hasToday", "缺失"),
    )
    if isinstance(records, list) and "records" in data:
        today = time.strftime("%Y-%m-%d")
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                record_ts = int(float(record.get("ts")))
            except (TypeError, ValueError):
                continue
            if time.strftime("%Y-%m-%d", time.localtime(record_ts)) == today:
                return True
        return False

    if "hasToday" in data:
        return bool(data.get("hasToday"))

    return None


def attendance_progress(data: Dict[str, Any], signed: Optional[bool]) -> str:
    """签到项的进度文案。**未签就绝不能显示"已签"**（旧版兜底文案的坑）。

    - 未签： "今日未签"
    - 已签： 尽量展示今天领到的奖励（"已领 名称*数量"），拿不到就 "已签"
    - 未知： 空串（调用方会按字段缺失报错）
    """
    if signed is False:
        return "今日未签"
    if signed is None:
        return ""

    # 已签：明日方舟按 records 里今天的记录拼奖励
    try:
        records = data.get("records") or []
        if isinstance(records, list):
            zero_ts = str(_today_zero_ts())
            today_records = [
                r for r in records
                if isinstance(r, dict) and str(r.get("ts", "")) == zero_ts
            ]
            if today_records:
                res_map = data.get("resourceInfoMap") or {}
                parts = []
                for r in today_records:
                    res = res_map.get(str(r.get("resourceId") or r.get("awardId") or "")) or {}
                    name = str(res.get("name") or "奖励")
                    parts.append(f"{name}*{r.get('count', 1)}")
                return "已领 " + "、".join(parts)
    except Exception:  # 奖励文案只是锦上添花
        pass
    return "已签"


def _today_zero_ts() -> int:
    """本地时区"今天零点"的 Unix 秒（与 sign-task 的 zero 时间戳口径一致）。"""
    return int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
