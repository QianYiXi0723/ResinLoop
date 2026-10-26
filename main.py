#!/usr/bin/env python3
"""游戏每日任务未完成提醒 —— 主流程入口（第 1 阶段：mock 数据源）。

流程：加载配置 -> 逐账号查询（mock）-> 汇总判断 -> 按测试模式决定是否推送
-> 写日志 -> 按退出码退出。不签到、不改任何游戏数据，只读 + 提醒。

用法：
    python main.py --init            # 首次运行，生成 config.json
    python main.py                   # 正常检查（按配置里的 test_mode）
    python main.py --force-alert     # 临时强制推送，验证 Bark 是否通
    python main.py --force-silent    # 临时强制静默，只看日志
    python main.py --dry-run         # 跑完整流程但不真的发请求

退出码：
    0 成功（推不推送都算成功）
    1 配置错误
    2 所有账号查询失败
    3 需要推送但全部通道推送失败
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.analyzer import build_report
from core.config import (
    TEST_MODE_FORCE_ALERT,
    TEST_MODE_FORCE_SILENT,
    AppConfig,
    ConfigError,
    create_default_config,
    load_config,
    next_check_time,
)
from core.logger import setup_logger
from core.models import GameStatus
from core.notifier import build_notifiers, push_all, should_push
from core.querier import UnknownProviderError, get_querier

DEFAULT_CONFIG_NAME = "config.json"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def resolve_path(raw: str) -> Path:
    """相对路径按项目根目录解析，保证在任意 cwd 下都能跑。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def _safe_console(text: str = "") -> None:
    """控制台输出，遇到编码不支持的字符也不至于崩掉。"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _print_error(text: str) -> None:
    """红色错误输出（Windows 10+ 终端支持 ANSI；不支持时退回普通输出）。"""
    try:
        print(f"\033[91m{text}\033[0m")
    except Exception:  # pragma: no cover
        _safe_console(text)


def _apply_mihoyo_query_config(config: AppConfig) -> None:
    """把 query.proxy / mihoyo_device_mode 下发给米游社访问层。

    必须在任何米游社请求之前调用：getFp 按出口 IP 签发指纹，代理若只在
    dailyNote 生效而 getFp 直连，拿到的仍是本机 IP 的黑指纹，等于没换。
    """
    from core import mihoyo_api
    from core.querier import set_mihoyo_task_scope

    mihoyo_api.set_proxy(config.query.proxy)
    mihoyo_api.set_device_mode(config.query.mihoyo_device_mode)
    set_mihoyo_task_scope(config.query.mihoyo_task_scope)


def _validate_skland_token(token: str) -> str:
    """校验登录 token 格式，返回 '' 表示通过，否则返回错误原因。

    约束来自真实接口实测（2026-10-26）：
      - `/user/oauth2/v2/grant` 的 GrantV2Req.token 有 max=128 长度校验
        （128 通过，129 起返回 `failed on the 'max' tag`）；
      - 正确 token 是 token_by_scan_code 返回的登录 token：40~100 字符、
        纯 base64 字符集、不以 eyJ 开头；
      - eyJ 开头是网页端 JWT（旧版流程误换的"长 token"），必然超长被拒。
    """
    from core.skland_api import MAX_TOKEN_LEN

    token = (token or "").strip()
    if not token:
        return "登录接口返回空 token"
    if any(ch.isspace() for ch in token):
        return f"token 中间含空白字符（长度 {len(token)}），疑似格式污染"
    if token.startswith("eyJ"):
        return (
            f"token 以 eyJ 开头（JWT，长度 {len(token)}），这是网页端 token，"
            "skland 换票接口只接受登录短 token"
        )
    if len(token) > MAX_TOKEN_LEN:
        return (
            f"token 长度 {len(token)} 超过接口上限 {MAX_TOKEN_LEN}（GrantV2Req.token max 校验），"
            "疑似取到了错误的长 token"
        )
    if len(token) < 16:
        return f"token 过短（长度 {len(token)}），不像是有效的登录 token"
    return ""


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #


def cmd_login_skland(config_path: Path, game: str) -> int:
    """扫码登录鹰角通行证，把拿到的长期 Token 写回 config.json 的指定游戏节点。

    【多账号支持】明日方舟和终末地可能在不同的鹰角通行证账号下：
    game="both" 时依次扫两次码，每次只更新对应游戏的 credential.token，
    绝不覆盖另一个游戏的 Token。
    """
    import json

    from core.hypergryph_login import HypergryphLoginError, run_qr_login

    games = ["arknights", "endfield"] if game == "both" else [game]
    results: List[str] = []

    for target in games:
        _safe_console("=" * 60)
        _safe_console(f"鹰角通行证扫码登录 -> {target}")
        _safe_console("=" * 60)
        try:
            result = run_qr_login()
        except HypergryphLoginError as exc:
            _safe_console(f"[登录失败] {target}：{exc}")
            return 1
        except KeyboardInterrupt:
            _safe_console("\n已中断")
            return 130

        token = (result["token"] or "").strip()
        _safe_console(f"登录成功！Token：长度 {len(token)}，前缀 {token[:10]!r}")

        # ---- 写入前的硬校验（格式不对绝不落盘） ---- #
        problem = _validate_skland_token(token)
        if problem:
            _print_error("登录接口获取 Token 异常，请检查源码：")
            _print_error(f"  - {problem}")
            _print_error(f"  - 实际长度={len(token)}，前缀={token[:16]!r}")
            _print_error("  - 已中止，未写入 config.json")
            return 1

        # ---- 语义预检：真的能过 grant 校验才写 ---- #
        from core.skland_api import verify_ark_token

        _safe_console("正在向鹰角 grant 接口验证 token 有效性...")
        check = verify_ark_token(token)
        if not check.get("ok"):
            _print_error("登录接口获取 Token 异常，请检查源码：")
            _print_error(f"  - {check.get('error')}")
            _print_error(f"  - token 长度={len(token)}，前缀={token[:16]!r}")
            _print_error("  - 已中止，未写入 config.json")
            return 1
        _safe_console("      验证通过（grant 返回 status=0）")

        # 只写回目标游戏的 credential.token，不动其它游戏
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            hit = False
            for account in raw.get("accounts", []):
                if account.get("game") == target:
                    account.setdefault("credential", {})["token"] = token
                    hit = True
                    break
            if not hit:
                _safe_console(f"config.json 里没有 {target} 账号节点，Token 未写入")
                return 1
            # 同上：模板的 provider=mock 会让人误以为在查真实数据，登录成功后切走。
            query_node = raw.setdefault("query", {})
            if str(query_node.get("provider", "")).strip().lower() == "mock":
                query_node["provider"] = "miyoushe"
                _safe_console("已自动把 query.provider 从 mock 切到 miyoushe（否则查的是模拟数据）")
            config_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            # 清掉该游戏的旧 cred 缓存，强制下次用新 token 重新换取
            cache_file = BASE_DIR / "logs" / f"skland_cred_{target}.json"
            cache_file.unlink(missing_ok=True)
            _safe_console(f"已写入 config.json 的 {target} 节点（未触碰其它游戏），下次运行自动生效")
            results.append(target)
        except (OSError, ValueError) as exc:
            _print_error(f"[配置写入失败] {exc}")
            _safe_console("请手动把下面的 Token 填到 config.json 对应游戏的 credential.token：")
            _safe_console(token)
            return 1

    if len(results) > 1:
        _safe_console(f"全部完成：{'、'.join(results)} 的 Token 均已分别写入")
    return 0


def cmd_login_mihoyo(config_path: Path, proxy: Optional[str] = None) -> int:
    """米游社扫码登录：手机米游社 App 扫码，自动写入 Cookie 并绑定设备。

    一次扫码更新原神 / 崩铁 / 绝区零三个账号（同一米游社账号），
    并自动把 query.mihoyo_enabled 改回 true。
    """
    import json

    from core.mihoyo_login import MiyousheLoginError, run_qr_login

    _safe_console("=" * 60)
    _safe_console("米游社扫码登录（原神 / 崩铁 / 绝区零）")
    _safe_console("=" * 60)

    # 先下发代理：扫码登录的出口 IP 应与后续查询一致，否则"登录设备"和
    # "查询设备"处在两个网络环境下，服务端仍会判定异常。
    try:
        _login_config = load_config(config_path, auto_create=False)
        if proxy is not None:
            _login_config.query.proxy = str(proxy).strip()
        setup_logger(_login_config.logging, base_dir=BASE_DIR)
        _apply_mihoyo_query_config(_login_config)
    except ConfigError as exc:
        _safe_console(f"[配置错误] {exc}")
        return 1

    try:
        result = run_qr_login()
    except MiyousheLoginError as exc:
        _safe_console(f"[登录失败] {exc}")
        return 1
    except KeyboardInterrupt:
        _safe_console("\n已中断")
        return 130

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        updated: List[str] = []
        for account in raw.get("accounts", []):
            if account.get("game") in ("genshin", "starrail", "zzz"):
                credential = account.setdefault("credential", {})
                credential["cookie"] = result["cookie"]
                # 清掉可能残留的旧分字段，避免覆盖整串 cookie 的正确值
                credential.pop("ltuid", None)
                credential.pop("ltoken", None)
                updated.append(str(account.get("nickname") or account.get("game")))
        if not updated:
            _safe_console("config.json 里没有米游社三款游戏的账号节点，Cookie 未写入")
            return 1

        query_node = raw.setdefault("query", {})
        query_node["mihoyo_enabled"] = True
        # 模板配置默认 provider=mock（演示数据）。既然扫码登录了真实账号，
        # 就顺手切到 miyoushe，避免"登录成功却还在看模拟数据"的假象。
        switched = False
        if str(query_node.get("provider", "")).strip().lower() == "mock":
            query_node["provider"] = "miyoushe"
            switched = True
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        _safe_console(f"[配置写入失败] {exc}")
        _safe_console("请手动把下面的 Cookie 填到三款游戏的 credential.cookie：")
        _safe_console(result["cookie"])
        return 1

    _safe_console(f"已写入 config.json（账号：{'、'.join(updated)}）")
    _safe_console(f"设备已绑定：device_id={result['device_id'][:8]}…（三款游戏共用）")
    _safe_console("已自动把 query.mihoyo_enabled 改回 true")
    if switched:
        _safe_console("已自动把 query.provider 从 mock 切到 miyoushe（否则查的是模拟数据）")
    _safe_console("下一步建议：python main.py --dry-run --debug 验证是否还报 5003")
    return 0


def cmd_probe_mihoyo(config_path: Path, debug: bool = False, proxy: Optional[str] = None) -> int:
    """米游社链路探针：出口 IP -> getFp -> deviceLogin/saveDevice -> dailyNote。

    专为"换代理试 IP"设计：一次跑完就能看清
      1) 代理是否真的生效（出口 IP）；
      2) 新 IP 签发的 device_fp 是什么（和旧的 38d7f191… 对比）；
      3) 设备注册是否成功；
      4) 带设备头（auto）与不带设备头（gs）两种请求各自的 retcode。
    """
    from core import mihoyo_api as m

    try:
        config = load_config(config_path, auto_create=False)
    except ConfigError as exc:
        _safe_console(f"[配置错误] {exc}")
        return 1
    if proxy is not None:
        config.query.proxy = str(proxy).strip()
    if debug:
        config.logging.level = "DEBUG"
    setup_logger(config.logging, base_dir=BASE_DIR)
    _apply_mihoyo_query_config(config)

    _safe_console("=" * 66)
    _safe_console("米游社查询链路探针（只读：getFp / 设备注册 / dailyNote）")
    _safe_console("=" * 66)
    _safe_console(f"代理      ：{config.query.proxy or '直连'}")
    _safe_console(f"设备模式  ：{config.query.mihoyo_device_mode}（auto=GsCore 式 + 风控重试）")

    # 未显式配置代理时，requests 会自动沿用系统 HTTP(S)_PROXY。
    # 这时"直连"其实也走了代理，出口 IP 不变，getFp 自然还是同一个指纹。
    if not config.query.proxy:
        import os

        env_proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if env_proxy:
            _safe_console(
                f"系统代理  ：{env_proxy}（未配 query.proxy，requests 会自动沿用，"
                "出口 IP 不会变）"
            )

    # 1) 出口 IP（确认代理真的生效）
    try:
        import requests

        resp = requests.get(
            "https://api.ipify.org",
            params={"format": "json"},
            proxies=m._proxies(),
            timeout=8,
        )
        _safe_console(f"出口 IP   ：{resp.json().get('ip') or resp.text[:40]}")
    except Exception as exc:
        _safe_console(f"出口 IP   ：获取失败（{exc}）——代理可能不可用")

    accounts = [a for a in config.enabled_accounts if a.game in ("genshin", "starrail", "zzz")]
    if not accounts:
        _safe_console("没有任何启用的米游社账号（被 enabled=false 或 mihoyo_enabled=false 跳过）")
        return 1
    account = accounts[0]
    try:
        ltuid, ltoken, uid, server, cookie = m.extract_credentials(account)
    except Exception as exc:
        _safe_console(f"凭证读取失败：{exc}")
        return 1

    fields = "、".join(sorted(m.parse_cookie_string(cookie))) or "ltuid/ltoken"
    _safe_console(f"测试账号  ：{account.game_name}（uid={uid}，server={server}）")
    _safe_console(f"Cookie 字段：{fields}")
    if "cookie_token=" not in cookie:
        _safe_console(
            "提示      ：当前 Cookie 只有 ltuid/ltoken，缺少 cookie_token。"
            "GsCore 用户通常带此字段；建议跑 --login-mihoyo 重新扫码（新版会保留 v2 字段）"
        )

    # 2) getFp（按当前出口 IP 签发）
    device_id, device_fp, _ua = m._get_device_info(account.game)
    _safe_console(f"device_id ：{device_id}")
    _safe_console(f"device_fp ：{device_fp}")

    # 3) 设备注册（deviceLogin -> saveDevice）
    try:
        results = m.device_login_and_save(ltuid, ltoken, device_id, device_fp, cookie=cookie)
        login_code = (results.get("login") or {}).get("retcode")
        save_code = (results.get("save") or {}).get("retcode")
        _safe_console(f"设备注册  ：deviceLogin={login_code}，saveDevice={save_code}")
    except Exception as exc:
        _safe_console(f"设备注册  ：异常（{exc}）")

    # 4) dailyNote：生产路径（auto）+ 无设备头对照（gs）
    for mode, label in (("auto", "auto 生产路径"), ("gs", "gs 无设备头对照")):
        try:
            data = m.request_daily_note(
                game_id=account.game,
                ltuid=ltuid,
                ltoken=ltoken,
                uid=uid,
                server=server,
                cookie=cookie,
                mode=mode,
            )
            preview = "，".join(
                f"{key}={data[key]}" for key in list(data)[:4]
            )
            _safe_console(f"[{label}] ✅ retcode=0｜{preview}")
        except Exception as exc:
            _safe_console(f"[{label}] ❌ {exc}")

    _safe_console("-" * 66)
    _safe_console("如果换了代理后 device_fp 仍是 38d7f191…，说明 getFp 没走代理或该 IP 仍是出口 IP。")
    return 0


def cmd_init(config_path: Path) -> int:
    """生成默认配置，已存在则不覆盖。"""
    if config_path.exists():
        _safe_console(f"配置文件已存在，未覆盖：{config_path}")
        return 0
    create_default_config(config_path)
    _safe_console(f"已生成默认配置：{config_path}")
    _safe_console("下一步：填 push.bark.device_key（Bark App 首页那串 Key），再运行 python main.py")
    return 0


def collect_statuses(config: AppConfig) -> List[GameStatus]:
    """遍历启用账号，逐个查询；单个账号失败不影响其它账号。"""
    import logging

    from core.config import MIHOYO_GAMES

    logger = logging.getLogger("dailycheck.main")
    accounts = config.enabled_accounts

    # 米游社整体禁用开关：跳过原神/崩铁/绝区零（不算失败，不触发"全部查询失败"退出码）
    if config.query.provider == "miyoushe" and not config.query.mihoyo_enabled:
        skipped = [a for a in accounts if a.game in MIHOYO_GAMES]
        accounts = [a for a in accounts if a.game not in MIHOYO_GAMES]
        if skipped:
            logger.warning(
                "米游社查询已在配置中禁用（query.mihoyo_enabled=false），跳过：%s",
                "、".join(a.game_name for a in skipped),
            )

    if not accounts:
        logger.warning("没有任何可查询的账号（accounts 全部被禁用、为空或被米游社开关跳过）")
        return []

    logger.info(
        "开始检查：%d 个账号，数据源=%s，测试模式=%s",
        len(accounts),
        config.query.provider,
        config.test_mode,
    )

    statuses: List[GameStatus] = []
    for account in accounts:
        try:
            querier = get_querier(account.game, provider=config.query.provider)
            status = querier.fetch(account)
        except UnknownProviderError as exc:
            logger.error("查询器不可用：%s", exc)
            status = GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                error=str(exc),
            )
        except Exception as exc:  # 兜底，绝不让查询异常打断整轮
            logger.exception("%s 查询异常", account.game_name)
            status = GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                error=f"查询异常：{exc}",
            )

        level = logging.INFO if status.ok else logging.WARNING
        logger.log(
            level,
            "%s(%s) -> %s",
            status.game_name,
            status.display_name,
            "全部完成" if status.finished else (
                f"未完成 {len(status.unfinished_tasks)} 项" if status.ok else f"查询失败：{status.error}"
            ),
        )
        statuses.append(status)
    return statuses


def run_once(config: AppConfig, dry_run: bool = False) -> int:
    """跑一轮完整检查：查询 -> 判断 -> 推送 -> 返回退出码。

    命令行（cmd_run）和守护进程（daemon.py）共用这一份逻辑，避免两处行为漂移。
    退出码含义与模块文档一致。
    """
    import logging

    logger = logging.getLogger("dailycheck.main")

    # 1) 查询
    statuses = collect_statuses(config)

    # 2) 判断（体力溢出预警复用本轮查询结果，不额外发请求）
    report = build_report(statuses, config.resource_thresholds)
    _safe_console(report.to_console_text())
    logger.info(
        "汇总：总数=%d 已完成=%d 未完成=%d 失败=%d 漏做任务=%d 体力预警=%d",
        len(report.statuses),
        len(report.finished),
        len(report.unfinished),
        len(report.failed),
        report.unfinished_task_count,
        len(report.resource_alerts),
    )
    for alert in report.resource_alerts:
        logger.info("体力预警：%s", alert.warn_line())

    # 2.5) 落一份历史（dry-run 不算，免得污染统计）
    if not dry_run:
        try:
            from core import history

            history.record(report)
        except Exception as exc:  # noqa: BLE001
            # 存档失败绝不能影响主流程：日志里说一声就好
            logger.warning("写入历史记录失败（不影响本轮检查）：%s", exc)

    # 3) 推送决策
    alert, reason = should_push(
        config.test_mode,
        report.need_alert,
        len(report.unfinished),
        always_notify=config.push.always_notify,
        overflow_count=len(report.resource_alerts),
    )
    logger.info("推送决策：%s（%s）", "推送" if alert else "不推送", reason)

    push_results = []
    if alert:
        notifiers = build_notifiers(config.push)
        if not notifiers:
            logger.warning("push.channels 里没有可用通道，本次不推送")
        else:
            title, body = report.to_push_payload()
            logger.info("推送标题：%s", title)
            push_results = push_all(notifiers, title, body, dry_run=dry_run)
            for result in push_results:
                logger.info("推送结果 %s", result)

    # 4) 收尾 + 退出码
    if statuses and all(not status.ok for status in statuses):
        logger.error("所有账号查询失败，请检查凭证或数据源配置")
        logger.info("=" * 56)
        return 2

    if alert and push_results and not any(r.ok for r in push_results):
        if all(r.skipped for r in push_results):
            logger.warning("没有任何通道真正发出消息（配置不全或 dry-run），本次算作已提醒但未送达")
        else:
            logger.error("推送全部失败，请检查网络或 Bark 配置")
            logger.info("=" * 56)
            return 3

    logger.info("本轮检查结束")
    logger.info("=" * 56)
    return 0


def cmd_run(config_path: Path, args: argparse.Namespace) -> int:
    """主流程。"""
    import logging

    try:
        config = load_config(config_path, auto_create=False)
    except ConfigError as exc:
        _safe_console(f"[配置错误] {exc}")
        return 1

    # 命令行 --proxy 优先于配置（只影响本次运行）
    if getattr(args, "proxy", None) is not None:
        config.query.proxy = str(args.proxy).strip()

    # 命令行参数临时覆盖测试模式，不写回配置文件
    if args.force_alert:
        config.test_mode = TEST_MODE_FORCE_ALERT
    elif args.force_silent:
        config.test_mode = TEST_MODE_FORCE_SILENT

    # 调试模式：把日志级别临时提到 DEBUG（不写回配置文件）
    if args.debug:
        config.logging.level = "DEBUG"
    logger = setup_logger(config.logging, base_dir=BASE_DIR)
    # 代理 / 设备模式必须在任何米游社请求之前下发（getFp 按出口 IP 签发指纹）
    _apply_mihoyo_query_config(config)
    if args.debug:
        logger.info("调试模式已开启：将输出接口原始返回（脱敏后），供核对真实数据来源")
    logger.info("=" * 56)
    logger.info("每日任务检查启动｜配置=%s", config_path.name)
    logger.info(
        "检查时间点：%s｜下次：%s",
        "、".join(config.check_times),
        next_check_time(config.check_times).strftime("%m-%d %H:%M"),
    )
    logger.info("测试模式：%s", config.test_mode_label)
    if args.dry_run:
        logger.info("dry-run 已开启：不会发起真实推送请求")

    return run_once(config, dry_run=args.dry_run)



# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dailycheck",
        description="游戏每日任务未完成提醒（第 1 阶段：mock 数据 + Bark 推送）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py --init\n"
            "  python main.py\n"
            "  python main.py --force-alert --dry-run\n"
        ),
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_NAME,
        help=f"配置文件路径，默认 {DEFAULT_CONFIG_NAME}（相对项目根目录）",
    )
    parser.add_argument("--init", action="store_true", help="生成默认配置文件后退出")
    parser.add_argument(
        "--login-skland", nargs="?", const="both", default=None,
        choices=("arknights", "endfield", "both"),
        metavar="{arknights|endfield}",
        help="扫码登录鹰角通行证并写入 config.json。可选参数指定游戏："
             "--login-skland arknights / --login-skland endfield /"
             " 不带参数=两款依次各扫一次（明日方舟与终末地可在不同账号下）",
    )
    parser.add_argument(
        "--login-mihoyo", action="store_true",
        help="米游社扫码登录：手机米游社 App 扫码，自动写入三款游戏 Cookie、"
             "绑定设备指纹，并把 mihoyo_enabled 改回 true",
    )
    parser.add_argument(
        "--probe-mihoyo", action="store_true",
        help="米游社链路探针：打印出口 IP / 新签发的 device_fp / 设备注册结果，"
             "并对比带设备头与不带设备头两种请求的 retcode（换代理后先跑这个）",
    )
    parser.add_argument(
        "--proxy", default=None, metavar="URL",
        help="临时覆盖 query.proxy（如 http://127.0.0.1:7890），只对本次运行生效。"
             "换 IP 试风控时用：--probe-mihoyo --proxy http://127.0.0.1:7890",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--force-alert", action="store_true", help="临时强制提醒（覆盖配置里的 test_mode）"
    )
    mode_group.add_argument(
        "--force-silent", action="store_true", help="临时强制静默（覆盖配置里的 test_mode）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="完整跑流程但不真正发推送请求"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="调试模式：日志级别调到 DEBUG，打印接口原始返回（已脱敏），用于排查数据来源",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # 控制台统一 UTF-8，避免 Windows GBK 终端打不出中文
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass

    args = build_parser().parse_args(argv)
    config_path = resolve_path(args.config)

    try:
        if args.init:
            return cmd_init(config_path)
        if args.login_skland:
            return cmd_login_skland(config_path, args.login_skland)
        if args.login_mihoyo:
            return cmd_login_mihoyo(config_path, proxy=args.proxy)
        if args.probe_mihoyo:
            return cmd_probe_mihoyo(config_path, debug=args.debug, proxy=args.proxy)
        return cmd_run(config_path, args)
    except KeyboardInterrupt:
        _safe_console("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
