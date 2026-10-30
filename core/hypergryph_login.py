"""鹰角通行证扫码登录（获取 skland 换票用的登录 Token）。

流程（接口抄自社区开源实现 Feonil/Arknights-Query-for-Endfield-ID 的
hypergryph_login.py，并与 starudream/sign-task 的登录链路交叉验证）：

    1. POST as.hypergryph.com/general/v1/gen_scan/login          （空 JSON body）
       -> data: {scanId, scanUrl, enableScanAppList}
    2. GET  as.hypergryph.com/general/v1/scan_status?scanId=...   （轮询，2 秒一次）
       -> status: 100=未扫码 101=已扫码待确认 0=已确认(返回 scanCode) 3/4=取消/失败
    3. POST as.hypergryph.com/user/auth/v1/token_by_scan_code     {"scanCode": ...}
       -> data: {token: 登录 token（**这就是 config.json 要填的 token**）}

【重要纠错 2026-10-26】旧版本在第 3 步之后又调了一次
`/user/oauth2/v2/grant`（appCode=be36d44aa36bfb5b, type=1）去换所谓"长 token"，
把那个 176 字符的结果写进配置 —— 但 skland 的 `/user/oauth2/v2/grant`
对 `GrantV2Req.token` 有 **max=128** 的长度校验（实测：128 通过，129 即报
`failed on the 'max' tag`），必然失败。

结论（与 sign-task pkg/skland/job/auth.go 一致）：
    传给 skland grant 的 token，就是 token_by_scan_code 返回的**登录 token**，
    典型长度 40~100 字符、纯 base64 字符集，**不以 eyJ 开头**（eyJ 是网页端 JWT）。

二维码渲染：优先用 qrcode 库在终端打印 ASCII 二维码 + 保存 qr_login.png；
qrcode 未安装时降级为把 scanUrl 打出来让用户自行生成二维码。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .skland_api import ADDR_HYPERGRYPH, MAX_TOKEN_LEN, _import_requests

logger = logging.getLogger("dailycheck.hglogin")

# --------------------------------------------------------------------------- #
# 常量（全部来自社区实测实现，非猜测）
# --------------------------------------------------------------------------- #

GEN_SCAN_URL = ADDR_HYPERGRYPH + "/general/v1/gen_scan/login"
SCAN_STATUS_URL = ADDR_HYPERGRYPH + "/general/v1/scan_status"
TOKEN_BY_SCAN_URL = ADDR_HYPERGRYPH + "/user/auth/v1/token_by_scan_code"

#: scan_status 的业务状态码
SCAN_WAITING = 100        # 未扫码
SCAN_CONFIRMING = 101     # 已扫码，等待手机上确认
SCAN_CONFIRMED = 0        # 已确认，data 里带 scanCode
SCAN_CANCELED = 3         # 用户取消
SCAN_EXPIRED = 4          # 二维码过期

#: 轮询间隔（秒）与默认超时（秒）
POLL_INTERVAL = 2
DEFAULT_TIMEOUT = 180

#: 二维码图片输出路径（项目根目录）
QR_PNG_PATH = Path(__file__).resolve().parent.parent / "qr_login.png"

#: 请求头：模拟鹰角通行证网页端（与社区实现一致）
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://user.hypergryph.com",
    "Referer": "https://user.hypergryph.com/",
}


class HypergryphLoginError(Exception):
    """扫码登录任一步失败（网络 / 业务状态码 / 超时 / 取消 / token 格式非法）。"""


# --------------------------------------------------------------------------- #
# 调试输出：把服务端完整返回打到控制台（排查 token 问题时必需）
# --------------------------------------------------------------------------- #


def notify_status(callback: Optional[Any], value: Any) -> None:
    """安全调用扫码状态回调。

    图形界面用它刷新"已扫码待确认"等中间态；回调属于外部代码，
    抛错不应中断登录流程，这里一律吞掉并记到 debug 日志。
    """
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001 - 外部回调
        logging.getLogger("dailycheck.hglogin").debug("状态回调异常（已忽略）", exc_info=True)


def dump_raw(label: str, payload: Any) -> None:
    """把服务端原始返回（完整 JSON）打印到控制台。

    只走 print（控制台），不写日志文件，避免把 token 落到磁盘日志里。
    """
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(payload)
    print(f"----- {label} 原始返回 -----")
    print(text)
    print("-" * (len(label) + 18))


def describe_token(token: str) -> str:
    """token 体检：长度 / 前缀 / 是否含空白，用于判断来源是否正确。"""
    return (
        f"长度={len(token)}，前缀={token[:10]!r}，含空白={any(c.isspace() for c in token)}"
    )


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #


def create_qr() -> Dict[str, Any]:
    """第 1 步：创建扫码会话。

    Returns:
        {"scan_id": str, "scan_url": str, "app_names": List[str]}

    Raises:
        HypergryphLoginError: 网络 / HTTP / 业务错误 / 字段缺失。
    """
    requests = _import_requests()
    try:
        resp = requests.post(GEN_SCAN_URL, json={}, headers=WEB_HEADERS, timeout=10)
    except Exception as exc:
        raise HypergryphLoginError(f"创建扫码会话网络异常：{exc}") from exc
    if resp.status_code != 200:
        raise HypergryphLoginError(f"创建扫码会话失败（HTTP {resp.status_code}），请稍后再试")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise HypergryphLoginError("创建扫码会话返回了无法解析的数据，请稍后再试") from exc

    if payload.get("status") != 0:
        dump_raw("gen_scan/login", payload)
        raise HypergryphLoginError(
            f"创建扫码会话失败：status={payload.get('status')}，msg={payload.get('msg')}"
        )
    data = payload.get("data") or {}
    scan_id = str(data.get("scanId") or "")
    scan_url = str(data.get("scanUrl") or "")
    if not (scan_id and scan_url):
        dump_raw("gen_scan/login", payload)
        raise HypergryphLoginError("创建扫码会话失败：服务端返回不完整，请稍后再试")

    app_names = [
        str(app.get("name") or "")
        for app in (data.get("enableScanAppList") or [])
        if isinstance(app, dict)
    ]
    dump_raw("gen_scan/login", payload)
    return {"scan_id": scan_id, "scan_url": scan_url, "app_names": [n for n in app_names if n]}


def poll_confirm(
    scan_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    on_status: Optional[Any] = None,
) -> str:
    """第 2 步：轮询扫码状态直到用户确认，返回 scanCode。

    on_status: 可选回调，状态变化时调用一次（供图形界面显示中间态）。

    Raises:
        HypergryphLoginError: 超时 / 用户取消 / 二维码过期 / 连续网络错误。
    """
    requests = _import_requests()
    url = f"{SCAN_STATUS_URL}?scanId={scan_id}"
    start = time.time()
    fail_count = 0
    last_status: Optional[int] = None

    while time.time() - start < timeout:
        try:
            resp = requests.get(url, headers=WEB_HEADERS, timeout=10)
            fail_count = 0
        except Exception as exc:
            fail_count += 1
            if fail_count >= 5:
                raise HypergryphLoginError(f"轮询扫码状态连续失败：{exc}") from exc
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            time.sleep(POLL_INTERVAL)
            continue

        try:
            payload = resp.json()
        except ValueError:
            time.sleep(POLL_INTERVAL)
            continue

        status = payload.get("status")
        data = payload.get("data") or {}

        # 状态变化时打印（每次轮询都打会刷屏，但每个状态都看得到）
        if status != last_status:
            last_status = status
            label = {
                SCAN_WAITING: "未扫码",
                SCAN_CONFIRMING: "已扫码，等待手机上确认",
                SCAN_CONFIRMED: "已确认",
                SCAN_CANCELED: "已取消",
                SCAN_EXPIRED: "已过期",
            }.get(status, f"未知状态({status})")
            print(f"[scan_status] 状态变为 {status}（{label}）")
            notify_status(on_status, status)

        if status == SCAN_CONFIRMED:
            scan_code = str(data.get("scanCode") or "")
            dump_raw("scan_status（已确认）", payload)
            if not scan_code:
                raise HypergryphLoginError("扫码确认失败：服务端返回不完整，请稍后再试")
            return scan_code
        if status in (SCAN_CANCELED, SCAN_EXPIRED):
            dump_raw("scan_status（取消/过期）", payload)
            label = "用户取消了登录" if status == SCAN_CANCELED else "二维码已过期"
            raise HypergryphLoginError(label)
        # 100 / 101 / 其它：继续等
        time.sleep(POLL_INTERVAL)

    raise HypergryphLoginError(f"等待扫码超时（{timeout} 秒）")


def exchange_short_token(scan_code: str) -> str:
    """第 3 步：scanCode -> 登录 token（**这就是 config.json 要填的 token**）。

    实测说明：该 token 典型长度 40~100 字符，纯 base64 字符集，不以 eyJ 开头。
    """
    requests = _import_requests()
    try:
        resp = requests.post(
            TOKEN_BY_SCAN_URL, json={"scanCode": scan_code}, headers=WEB_HEADERS, timeout=10
        )
    except Exception as exc:
        raise HypergryphLoginError(f"换取登录 token 网络异常：{exc}") from exc
    if resp.status_code != 200:
        raise HypergryphLoginError(f"换取登录凭证失败（HTTP {resp.status_code}），请稍后再试")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise HypergryphLoginError("换取登录凭证返回了无法解析的数据，请稍后再试") from exc
    dump_raw("token_by_scan_code", payload)
    if payload.get("status") != 0:
        raise HypergryphLoginError(
            f"换取登录 token 失败：status={payload.get('status')}，msg={payload.get('msg')}"
        )
    token = str((payload.get("data") or {}).get("token") or "").strip()
    if not token:
        raise HypergryphLoginError("换取登录 token 返回缺少 token 字段")
    print(f"[token_by_scan_code] {describe_token(token)}")
    return token


# --------------------------------------------------------------------------- #
# 二维码渲染
# --------------------------------------------------------------------------- #


def render_qr(scan_url: str) -> None:
    """终端打印 ASCII 二维码，并保存 qr_login.png（有 qrcode 库时）。

    qrcode 未安装时不抛错，降级为提示用户拿 scan_url 自行生成二维码。
    """
    try:
        import qrcode  # noqa: PLC0415 - 可选依赖
    except ImportError:
        print("未安装 qrcode 库，无法在终端渲染二维码。")
        print("可执行：pip install qrcode pillow")
        print(f"或把下面这串内容用任意在线二维码生成器转成二维码后，用手机 App 扫码：")
        print(scan_url)
        return

    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(scan_url)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print()

    try:
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(QR_PNG_PATH)
        print(f"二维码图片已保存：{QR_PNG_PATH}")
    except Exception as exc:  # 图片保存失败不影响扫码
        print(f"二维码图片保存失败（不影响终端扫码）：{exc}")


# --------------------------------------------------------------------------- #
# 一键流程
# --------------------------------------------------------------------------- #


def run_qr_login(timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """完整扫码登录流程。

    Returns:
        {"token": 登录 token（skland 换票直接用它）, "scan_id": ..., "app_names": [...]}

    注意：不再额外调 oauth2/v2/grant 换"长 token"——那个结果 176 字符，
    超过 grant 接口 max=128 的上限，skland 侧必然拒绝。

    Raises:
        HypergryphLoginError: 任一步失败。
    """
    session = create_qr()
    scan_url = session["scan_url"]
    apps = session["app_names"]
    print(f"[1/3] 二维码会话已创建（可用 App：{'、'.join(apps) or '明日方舟 / 森空岛 / 终末地'}）")
    render_qr(scan_url)
    print("请用手机上的 森空岛 / 明日方舟 / 终末地 App 扫描上方二维码并确认登录...")

    print("[2/3] 等待扫码确认（手机上点确认即继续）...")
    scan_code = poll_confirm(session["scan_id"], timeout=timeout)
    print(f"      已确认，拿到 scanCode（{scan_code[:8]}…）")

    print("[3/3] 换取登录 token...")
    token = exchange_short_token(scan_code)
    print(f"      拿到 token：{describe_token(token)}")

    return {"token": token, "scan_id": session["scan_id"], "app_names": apps}
