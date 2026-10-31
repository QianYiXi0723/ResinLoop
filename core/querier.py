"""查询层。

职责：给定账号配置，返回该账号当日的 GameStatus。
设计成 BaseQuerier + 注册表的形式：按 provider 分桶，按 game_id 找查询器。

第 2 阶段现状：
    mock      全部五款游戏（写死数据）
    miyoushe  原神 / 崩铁 / 绝区零走真实实时便笺接口（HTTP 细节见 core/mihoyo_api.py），
              明日方舟 / 终末地（森空岛）尚未接入，自动回退 mock。

主流程、判断器、推送器对"数据是真是假"无感知，三方拿到的都是同一套 TaskItem / GameStatus。
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type

from .config import KNOWN_GAMES, AccountConfig
from .history import GAME_DAY_OFFSET
from .mihoyo_api import (
    MiyousheAuthError,
    MiyousheNotBoundError,
    MiyousheQueryError,
    extract_credentials,
    request_daily_note,
    require_int,
)
from .models import GameStatus, TaskItem
from .skland_api import (
    GameNotBoundError,
    SklandAuthError,
    SklandQueryError,
    get_arknights_player_info,
    get_endfield_card_detail,
    get_skland_credentials,
    is_cred_expired_error,
    list_players,
)

logger = logging.getLogger("dailycheck.querier")

#: 米游社任务口径（query.mihoyo_task_scope）：
#:   daily = 只判「每日委托 / 每日实训 / 每日活跃度」，资源类仅展示
#:   full  = 树脂/体力/电量/派遣/周积分也参与完成度
_MIHOYO_TASK_SCOPE = "daily"


def set_mihoyo_task_scope(scope: str) -> str:
    """设置米游社任务口径（daily / full），返回生效值。非法值回退 daily。"""
    global _MIHOYO_TASK_SCOPE
    value = (scope or "daily").strip().lower()
    if value not in ("daily", "full"):
        logger.warning("未知的 mihoyo_task_scope=%r，回退 daily", scope)
        value = "daily"
    _MIHOYO_TASK_SCOPE = value
    logger.debug("米游社任务口径：%s", value)
    return value


def _resource_counted() -> bool:
    """资源类任务是否参与完成度判定（仅 full 模式为 True）。"""
    return _MIHOYO_TASK_SCOPE == "full"


def _optional_int(data: Any, *path: str) -> Optional[int]:
    """按路径取整数；任一级缺失或非数字返回 None。

    用于"锦上添花"的展示项（树脂、体力、派遣等）：字段改版时宁可不显示，
    也不要因为一个可选字段让整款游戏变成"查询失败"。
    """
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node or node[key] is None:
            return None
        node = node[key]
    try:
        return int(node)
    except (TypeError, ValueError):
        return None


def normalize_recover_seconds(value: Any, *, now: Optional[float] = None) -> Optional[int]:
    """把接口的"恢复"字段统一换算成**距满秒数**（None 原样返回）。

    各接口给的形态不一：
        米游社 recovery_time            剩余秒数（原神 resin_recovery_time / 崩铁 stamina_recover_time）
        终末地 detail.dungeon.maxTs     满员时刻的时间戳（秒或毫秒级，按量级判别）
    >1e12 视为毫秒时间戳、>1e9 视为秒时间戳（减 now 得剩余秒数），
    其余视为"已经是剩余秒数"直接用；时间戳在过去/已满返回 0。
    """
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    now_s = time.time() if now is None else now
    if ts > 1e12:      # 毫秒时间戳
        return max(0, int(ts / 1000 - now_s))
    if ts > 1e9:       # 秒时间戳
        return max(0, int(ts - now_s))
    return max(0, ts)  # 已是剩余秒数


class BaseQuerier:
    """查询器基类。子类实现 fetch()，并声明自己支持的 game_id。"""

    #: 数据来源标记，写进日志
    source = "base"

    def fetch(self, account: AccountConfig) -> GameStatus:
        raise NotImplementedError

    # ---- 子类可复用的小工具 ---- #
    @staticmethod
    def build_status(
        game_id: str,
        tasks: List[TaskItem],
        nickname: str = "",
        source: str = "mock",
    ) -> GameStatus:
        return GameStatus(
            game_id=game_id,
            game_name=KNOWN_GAMES.get(game_id, game_id),
            tasks=tasks,
            nickname=nickname,
            source=source,
        )


# --------------------------------------------------------------------------- #
# Mock 数据：写死的五种游戏结果，覆盖"全部完成"与"存在未完成"两种情况
# --------------------------------------------------------------------------- #

MOCK_TASKS: Dict[str, List[Dict[str, object]]] = {
    # 原神：漏了领每日奖励 + 树脂没用完 => 未完成
    "genshin": [
        {"name": "每日委托", "done": True, "progress": "4/4"},
        {"name": "领取每日奖励", "done": False, "progress": ""},
        {"name": "消耗原粹树脂", "done": False, "progress": "80/160"},
        {"name": "派遣探索", "done": True, "progress": "5/5"},
    ],
    # 崩铁：全部完成
    "starrail": [
        {"name": "每日实训", "done": True, "progress": "500/500"},
        {"name": "消耗开拓力", "done": True, "progress": "240/240"},
        {"name": "委托奖励", "done": True, "progress": ""},
    ],
    # 绝区零：差一张刮刮卡 => 未完成
    "zzz": [
        {"name": "每日委托", "done": True, "progress": "4/4"},
        {"name": "咖啡店营业", "done": True, "progress": ""},
        {"name": "刮刮卡", "done": False, "progress": ""},
    ],
    # 明日方舟：全部完成
    "arknights": [
        {"name": "每日任务", "done": True, "progress": ""},
        {"name": "剿灭作战", "done": True, "progress": "3/3"},
        {"name": "基建收菜", "done": True, "progress": ""},
    ],
    # 终末地：活跃度没打满 => 未完成
    "endfield": [
        {"name": "每日活跃度", "done": False, "progress": "60/100"},
        {"name": "据点物资收取", "done": True, "progress": ""},
    ],
}

#: mock 模式下未登记的游戏返回这套兜底数据，避免脚本因为新增游戏直接报错
MOCK_FALLBACK: List[Dict[str, object]] = [
    {"name": "每日任务", "done": False, "progress": "0/1"},
]


class MockQuerier(BaseQuerier):
    """返回写死结果的假查询器，完全不联网。"""

    source = "mock"

    def __init__(self, fallback_to_undone: bool = True) -> None:
        self.fallback_to_undone = fallback_to_undone

    def fetch(self, account: AccountConfig) -> GameStatus:
        raw_tasks = MOCK_TASKS.get(account.game)
        if raw_tasks is None:
            if not self.fallback_to_undone:
                return GameStatus(
                    game_id=account.game,
                    game_name=account.game_name,
                    nickname=account.nickname,
                    source=self.source,
                    error=f"mock 数据中未定义游戏 \"{account.game}\"",
                )
            raw_tasks = MOCK_FALLBACK

        tasks = [
            TaskItem(
                name=str(item.get("name", "")),
                done=bool(item.get("done", False)),
                progress=str(item.get("progress", "")),
            )
            for item in raw_tasks
        ]
        return self.build_status(
            game_id=account.game,
            tasks=tasks,
            nickname=account.nickname,
            source=self.source,
        )


# --------------------------------------------------------------------------- #
# 米游社真实查询器（第 2 阶段）：原神 / 崩铁 / 绝区零
# 森空岛系（明日方舟 / 终末地）本阶段不做，继续走 mock。
# --------------------------------------------------------------------------- #


class MiyousheQuerier(BaseQuerier):
    """米游社 game_record 实时便笺查询器（provider="miyoushe"）。

    一个类同时服务三款游戏：fetch(account) 按 account.game 分发到对应的
    端点与解析函数，注册时对 genshin / starrail / zzz 各挂一次即可。
    HTTP / DS / retcode 细节全部在 core/mihoyo_api.py，本类只做"字段 -> 任务"。

    失败语义（需求 6）：任何异常都以 MiyousheQueryError 抛出，由 main 统一
    标记为 GameStatus(error=...)，即"查询失败"，绝不会被当成"未完成"去推送；
    单款游戏失败不影响其它两款（main 本来就是逐账号隔离的）。
    """

    source = "miyoushe"

    #: 本查询器覆盖的游戏
    SUPPORTED_GAMES = ("genshin", "starrail", "zzz")

    def fetch(self, account: AccountConfig) -> GameStatus:
        if account.game not in self.SUPPORTED_GAMES:
            return GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                source=self.source,
                error=f"MiyousheQuerier 不支持游戏 {account.game}（应回退 mock，若看到本条说明注册表配置有误）",
            )
        return self._fetch_game(account)

    # ---------------- 主流程 ---------------- #
    def _fetch_game(self, account: AccountConfig) -> GameStatus:
        try:
            ltuid, ltoken, uid, server, cookie = extract_credentials(account)
            data = request_daily_note(
                game_id=account.game,
                ltuid=ltuid,
                ltoken=ltoken,
                uid=uid,
                server=server,
                cookie=cookie,
            )
            parser = self._PARSERS[account.game]
            tasks = parser(self, data)
        except MiyousheNotBoundError as exc:
            # 凭证有效但账号下没这款游戏的角色（或还没绑定角色 UID）：
            # 不是查询失败，不画红字、不触发凭证告警，等建号后自动生效。
            return GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                source=self.source,
                no_account=str(exc),
            )
        except (MiyousheAuthError, MiyousheQueryError):
            # 凭证/网络/retcode/字段缺失：原样上抛，main 会标记为查询失败
            raise
        except Exception as exc:  # 解析过程里的任何意外都收口成查询失败
            raise MiyousheQueryError(f"解析返回数据时出现未预期错误：{exc}") from exc

        return self.build_status(
            game_id=account.game,
            tasks=tasks,
            nickname=account.nickname,
            source=self.source,
        )

    # ---------------- 解析：原神 ---------------- #
    def parse_genshin(self, data: Any) -> List[TaskItem]:
        """原神实时便笺 -> 任务列表：每日委托、原粹树脂、探索派遣。

        字段以 2026-10-26 实测真实返回为准：
            每日委托  finished_task_num / total_task_num（旧版为 done_task，保留兼容）
            树脂      current_resin / max_resin + resin_recovery_time（距满秒数）
            派遣      current_expedition_num / max_expedition_num / expeditions[].status
        """
        tasks: List[TaskItem] = []

        finished = _optional_int(data, "finished_task_num")
        if finished is None:
            finished = _optional_int(data, "done_task")  # 旧版字段兼容
        total = _optional_int(data, "total_task_num")
        if finished is None or total is None:
            raise MiyousheQueryError("米游社返回的数据格式有变化，当前版本可能无法识别，请关注软件更新")
        # total=0（委托未解锁/接口没给数据）时不参与判定：0>=0 会退化成「已完成」
        # ——2026-10-31 朋友机器实证：久未游玩的账号被判「全部完成」。
        # 森空岛两侧同款护栏早已存在（total<=0 raise），这里补齐米游社侧
        if total > 0:
            tasks.append(TaskItem("每日委托", finished >= total, f"{finished}/{total}"))

        current_resin = _optional_int(data, "current_resin")
        max_resin = _optional_int(data, "max_resin")
        if current_resin is not None and max_resin:
            # 资源类：daily 口径下只展示，不参与完成度
            tasks.append(TaskItem(
                "原粹树脂", current_resin >= max_resin,
                f"{current_resin}/{max_resin}", counted=_resource_counted(),
                recover_seconds=normalize_recover_seconds(
                    _optional_int(data, "resin_recovery_time")),
            ))

        current_exp = _optional_int(data, "current_expedition_num")
        max_exp = _optional_int(data, "max_expedition_num")
        expeditions = data.get("expeditions") if isinstance(data, dict) else None
        if current_exp is not None and max_exp is not None and isinstance(expeditions, list):
            # 没有任何进行中的派遣 = 这项没事可做；有 Ongoing 就算未完成（该去收/补派遣了）。
            any_ongoing = any(
                isinstance(exp, dict) and str(exp.get("status") or "").lower() == "ongoing"
                for exp in expeditions
            )
            tasks.append(TaskItem(
                "探索派遣", not any_ongoing, f"{current_exp}/{max_exp}",
                counted=_resource_counted(),
            ))

        return tasks

    # ---------------- 解析：崩铁 ---------------- #
    def parse_starrail(self, data: Any) -> List[TaskItem]:
        """崩铁实时便笺 -> 任务列表：每日实训、开拓力、模拟宇宙。

        字段以 2026-10-26 实测真实返回为准：
            每日实训  current_train_score / max_train_score
            开拓力    current_stamina / max_stamina
            模拟宇宙  rogue_tourn_weekly_cur / rogue_tourn_weekly_max
                      （旧版为 rogue_tourned_info.weekly_acquired / weekly_max，保留兼容）
        """
        tasks: List[TaskItem] = []

        current_train = require_int(data, "current_train_score")
        max_train = require_int(data, "max_train_score")
        tasks.append(TaskItem("每日实训", current_train >= max_train, f"{current_train}/{max_train}"))

        current_stamina = _optional_int(data, "current_stamina")
        max_stamina = _optional_int(data, "max_stamina")
        if current_stamina is not None and max_stamina:
            tasks.append(TaskItem(
                "开拓力", current_stamina >= max_stamina, f"{current_stamina}/{max_stamina}",
                counted=_resource_counted(),
                recover_seconds=normalize_recover_seconds(
                    _optional_int(data, "stamina_recover_time")),
            ))

        rogue_cur = _optional_int(data, "rogue_tourn_weekly_cur")
        rogue_max = _optional_int(data, "rogue_tourn_weekly_max")
        if rogue_cur is None or rogue_max is None:
            rogue_cur = _optional_int(data, "rogue_tourned_info", "weekly_acquired")
            rogue_max = _optional_int(data, "rogue_tourned_info", "weekly_max")
        if rogue_cur is not None and rogue_max is not None:
            tasks.append(TaskItem(
                "模拟宇宙", rogue_cur >= rogue_max, f"{rogue_cur}/{rogue_max}",
                counted=_resource_counted(),
            ))

        return tasks

    # ---------------- 解析：绝区零 ---------------- #
    def parse_zzz(self, data: Any) -> List[TaskItem]:
        """绝区零实时便笺 -> 任务列表：每日活跃度、电量。

        字段以 2026-10-26 实测真实返回为准：
            每日活跃度  vitality.current / vitality.max
                        （旧版曾假设 vitality.progress.current / .max，保留兼容）
            电量        energy.progress.current / .max
        """
        tasks: List[TaskItem] = []

        vitality_now = _optional_int(data, "vitality", "current")
        vitality_max = _optional_int(data, "vitality", "max")
        if vitality_now is None or vitality_max is None:
            vitality_now = _optional_int(data, "vitality", "progress", "current")
            vitality_max = _optional_int(data, "vitality", "progress", "max")
        if vitality_now is None or vitality_max is None:
            raise MiyousheQueryError("米游社返回的数据格式有变化，当前版本可能无法识别，请关注软件更新")
        tasks.append(TaskItem("每日活跃度", vitality_now >= vitality_max, f"{vitality_now}/{vitality_max}"))

        energy_now = _optional_int(data, "energy", "progress", "current")
        energy_max = _optional_int(data, "energy", "progress", "max")
        if energy_now is not None and energy_max:
            tasks.append(TaskItem(
                "电量", energy_now >= energy_max, f"{energy_now}/{energy_max}",
                counted=_resource_counted(),
            ))

        return tasks

    _PARSERS = {
        "genshin": parse_genshin,
        "starrail": parse_starrail,
        "zzz": parse_zzz,
    }


# --------------------------------------------------------------------------- #
# 森空岛真实查询器：明日方舟 / 终末地
# 数据源：zonai.skland.com 的绑定角色 + 游戏内每日任务进度（只读）。
# 明日方舟：/api/v1/game/player/info -> data.routine.daily = {current, total}
# 终末地：  /api/v1/game/endfield/card/detail -> data.detail.dailyMission
#           = {dailyActivation, maxDailyActivation}
# HTTP / 签名细节在 core/skland_api.py，本类只做"接口数据 -> 任务"。
# --------------------------------------------------------------------------- #


class StaleSnapshotError(Exception):
    """森空岛返回的是「上次登录时的快照」，且快照早于今日 04:00 日界。

    这种快照不能当作"今日进度"：2026-10-30 07:26 实测，
    ``status.lastOnlineTs`` = 10-29 22:14（之后再没登录过游戏），
    而 ``routine.daily`` 仍原样返回 ``10/10`` —— 那是**前一天**的完成状态，
    理智同样停在快照时刻（25/205，实际早已回满）。

    注意它**不是查询失败**：凭证、网络、解析全都正常，只是游戏里今天还没刷新。
    调用方把它落到 ``GameStatus.stale``（数据待更新），而不是 ``error``。

    ``extra_tasks``：快照过期但仍然可信、值得展示的附带信息任务
    （如按恢复字段推算的理智），由调用方原样放进 GameStatus.tasks。
    """

    def __init__(self, reason: str, extra_tasks: Optional[List[TaskItem]] = None):
        super().__init__(reason)
        self.extra_tasks = list(extra_tasks or [])


def _stale_snapshot_reason(data: Dict[str, Any]) -> Optional[str]:
    """快照陈旧时返回给用户看的原因；新鲜 / 字段缺失返回 None。

    判据：``status.lastOnlineTs`` 早于"今日 04:00"（走 ``GAME_DAY_OFFSET``，
    与 :func:`core.history.game_date_key` 同口径，禁止各处自行算日子）。
    "现在"优先用服务端的 ``currentTs``，比本机时钟更可信。

    字段缺失时**不判定**：宁可沿用旧行为，也不因为一个可选字段
    把原本正常的查询打成失败。
    """
    now_ts = _optional_int(data, "currentTs")
    online_ts = _optional_int(data, "status", "lastOnlineTs")
    if not now_ts or not online_ts:
        return None
    reset_dt = (datetime.fromtimestamp(now_ts) - GAME_DAY_OFFSET).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + GAME_DAY_OFFSET
    if online_ts >= int(reset_dt.timestamp()):
        return None
    return (
        f"游戏数据未更新（上次在线 {datetime.fromtimestamp(online_ts):%m-%d %H:%M}），"
        "无法确认今日进度"
    )


def _arknights_sanity_task(data: Dict[str, Any]) -> Optional[TaskItem]:
    """明日方舟理智：优先按恢复字段现算，字段缺失回退快照值。

    理智恢复是确定性的（6 分钟/点），``status.ap.completeRecoveryTime``
    给出回满时刻，任意时刻的真实理智都能算出来——官方森空岛 App 同款
    口径（2026-10-31 与官方显示两次逐分钟对账：47@05:34、48@05:39:27）。
    "现在"用服务端 currentTs，不依赖本机时钟；快照冻结多久都不影响
    推算准确性。回退路径（无恢复字段）沿用快照值=旧行为，可选字段
    缺失绝不把查询打成失败。``recover_seconds`` 仅在现算路径有值，
    供调用方区分"推算值"与"快照值"。
    """
    status = data.get("status")
    ap = status.get("ap") if isinstance(status, dict) else None
    if not isinstance(ap, dict):
        return None
    ap_max = _optional_int(ap, "max")
    if not ap_max:
        return None
    now_ts = _optional_int(data, "currentTs")
    crt = _optional_int(ap, "completeRecoveryTime")
    cur = _optional_int(ap, "current")
    if crt and now_ts:
        # normalize 统一吃秒/毫秒并给出"距满秒数"，推算与 recover_seconds 同源
        recover_seconds = normalize_recover_seconds(crt, now=now_ts)
        if recover_seconds is None:
            projected = cur
        elif recover_seconds <= 0:
            projected, recover_seconds = ap_max, 0
        else:
            projected = max(0, ap_max - math.ceil(recover_seconds / 360))
    else:
        projected, recover_seconds = cur, None
    if projected is None:
        return None
    projected = max(0, min(projected, ap_max))
    return TaskItem(
        "理智", projected >= ap_max, f"{projected}/{ap_max}", counted=False,
        recover_seconds=recover_seconds,
    )


class SklandQuerier(BaseQuerier):
    """森空岛查询器（内部 provider="skland"，跟随任意主 provider 自动挂载）。

    查询流程：binding（验证凭证 + 取角色）-> 按游戏取"游戏内每日任务进度"。
    明日方舟取 routine.daily（每日任务 current/total），
    终末地取 dailyMission（每日活跃度 dailyActivation/maxDailyActivation）。
    """

    source = "skland"

    SUPPORTED_GAMES = ("arknights", "endfield")

    def fetch(self, account: AccountConfig) -> GameStatus:
        if account.game not in self.SUPPORTED_GAMES:
            return GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                source=self.source,
                error=f"SklandQuerier 不支持游戏 {account.game}",
            )
        try:
            tasks = self._fetch_game(account)
        except StaleSnapshotError as exc:
            # 快照过期**不是**查询失败：凭证、网络、解析都没问题，
            # 只是游戏里今天还没刷新。落到 stale（数据待更新）而不是 error ——
            # 前端按"待更新"中性展示，不画红字"失败"、不算进失败数；
            # 同时 unfinished_tasks 为空，既不会冒充"今日已完成"，
            # 也不会误报"未完成"去推送/记漏。extra_tasks 是 stale 期间
            # 仍然可信的附带信息（如按恢复推算的理智）。
            return GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                source=self.source,
                stale=str(exc),
                tasks=list(exc.extra_tasks),
            )
        except GameNotBoundError as exc:
            # 凭证有效但该森空岛账号下没这款游戏的角色（勾选并扫码了、
            # 但没玩过它）：同样不是查询失败，等建号后自动生效。
            return GameStatus(
                game_id=account.game,
                game_name=account.game_name,
                nickname=account.nickname,
                source=self.source,
                no_account=str(exc),
            )
        except (SklandAuthError, SklandQueryError):
            raise
        except Exception as exc:
            raise SklandQueryError(f"解析返回数据时出现未预期错误：{exc}") from exc

        return self.build_status(
            game_id=account.game,
            tasks=tasks,
            nickname=account.nickname,
            source=self.source,
        )

    def _fetch_game(self, account: AccountConfig) -> List[TaskItem]:
        """取凭证 -> 查游戏内进度；凭证过期（401）时强制重换并重试一次。

        森空岛 cred 是**短期凭证**（实测数小时即失效，返回 HTTP 401 +
        code 10000「请求异常」）。缓存 TTL 之外还可能提前失效，所以保留这层兜底：
        401 -> 重新换取 cred -> 重试一次，成功则把新 cred 写回缓存。
        """
        # game 传入缓存文件名后缀：arknights/endfield 凭证缓存分文件隔离，
        # 支持两款游戏分属两个鹰角通行证账号（token 不同、cred 不同）。
        credential = get_skland_credentials(account.credential or {}, account.game)
        try:
            return self._fetch_game_with(account, credential)
        except SklandQueryError as exc:
            if not is_cred_expired_error(exc):
                raise
            logger.warning(
                "[skland] %s 凭证疑似过期（%s），重新换取后重试一次",
                account.game,
                str(exc)[:80],
            )
            credential = get_skland_credentials(
                account.credential or {}, account.game, force_refresh=True
            )
            return self._fetch_game_with(account, credential)

    def _fetch_game_with(
        self, account: AccountConfig, credential: Dict[str, str]
    ) -> List[TaskItem]:
        players = list_players(credential["cred"], credential["token"])

        binding = self._find_binding(players, account.game)
        nickname = str(binding.get("nickName") or "")
        role = binding.get("defaultRole") if isinstance(binding.get("defaultRole"), dict) else {}

        # 展示玩家昵称优先角色昵称
        display = str(role.get("nickname") or "") or nickname
        if display:
            account.nickname = display

        if account.game == "arknights":
            uid = require_str(binding, "uid")
            data = get_arknights_player_info(credential["cred"], credential["token"], uid)
            # 森空岛给的是"上次登录时的快照"，跨过 04:00 日界就不能当今日进度用
            reason = _stale_snapshot_reason(data)
            if reason:
                # 快照过期但理智是确定性恢复量、照常可算（官方 App 同款口径）：
                # 附进 stale 文案并在 tasks 里带上推算值，页面在"无法确认
                # 今日进度"之外仍有可用信息；字段缺失则维持原横幅（旧行为）
                sanity = _arknights_sanity_task(data)
                extra = (sanity if sanity is not None
                         and sanity.recover_seconds is not None else None)
                if extra is not None:
                    now_ts = _optional_int(data, "currentTs") or 0
                    eta = datetime.fromtimestamp(now_ts + extra.recover_seconds)
                    reason += (f"。理智按恢复推算约 {extra.progress}，"
                               f"预计 {eta:%H:%M} 回满")
                raise StaleSnapshotError(reason, extra_tasks=[extra] if extra else None)
            return self.parse_arknights(data)

        role_id = str(role.get("roleId") or "")
        server_id = str(role.get("serverId") or "")
        if not (role_id and server_id):
            raise SklandQueryError("返回数据缺少终末地角色信息（defaultRole.roleId/serverId）")
        detail = get_endfield_card_detail(
            credential["cred"], credential["token"], role_id, server_id
        )
        return self.parse_endfield(detail)

    @staticmethod
    def parse_arknights(data: Dict[str, Any]) -> List[TaskItem]:
        """明日方舟 player/info -> 每日任务（data.routine.daily）+ 理智（data.status.ap）。"""
        routine = data.get("routine")
        if not isinstance(routine, dict):
            raise SklandQueryError("明日方舟返回数据缺少 routine 字典")
        daily = routine.get("daily")
        if not isinstance(daily, dict):
            raise SklandQueryError("明日方舟返回数据缺少 routine.daily")
        current = _require_int_field(daily, "current", "routine.daily.current")
        total = _require_int_field(daily, "total", "routine.daily.total")
        if total <= 0:
            raise SklandQueryError(f"routine.daily.total 异常（{total}），无法判断完成度")
        tasks = [TaskItem("每日任务", done=current >= total, progress=f"{current}/{total}")]

        # 理智：status.ap = {current, max}。可选字段，缺了宁可不展示，不让整款游戏失败。
        # 理智：按恢复字段现算（官方森空岛 App 同款口径），字段缺失回退快照值；
        # 可选字段，缺了宁可不展示，不让整款游戏变成"查询失败"
        sanity = _arknights_sanity_task(data)
        if sanity is not None:
            tasks.append(sanity)
        return tasks

    @staticmethod
    def parse_endfield(detail: Dict[str, Any]) -> List[TaskItem]:
        """终末地 card/detail -> 每日活跃度（dailyMission）+ 理智（dungeon）。

        理智在 ``detail.dungeon = {curStamina, maxStamina, maxTs}``，
        注意这两个值是**字符串**（实测 "91"/"350"），要按字符串转 int，
        不能直接当数字用（int("350") 可以，但 int 里套字符串要小心）。
        """
        mission = detail.get("dailyMission")
        if not isinstance(mission, dict):
            raise SklandQueryError("终末地返回数据缺少 dailyMission 字典")
        current = _require_int_field(
            mission, "dailyActivation", "dailyMission.dailyActivation"
        )
        total = _require_int_field(
            mission, "maxDailyActivation", "dailyMission.maxDailyActivation"
        )
        if total <= 0:
            raise SklandQueryError(f"dailyMission.maxDailyActivation 异常（{total}）")
        tasks = [TaskItem("每日活跃度", done=current >= total, progress=f"{current}/{total}")]

        # 理智：可选字段，缺了宁可不展示，不让整款游戏变成"查询失败"
        dungeon = detail.get("dungeon")
        if isinstance(dungeon, dict):
            cur = _optional_int(dungeon, "curStamina")
            max_ = _optional_int(dungeon, "maxStamina")
            if cur is not None and max_:
                tasks.append(TaskItem(
                    "理智", cur >= max_, f"{cur}/{max_}", counted=False,
                    recover_seconds=normalize_recover_seconds(
                        _optional_int(dungeon, "maxTs")),
                ))
        return tasks

    @staticmethod
    def _find_binding(players: Dict[str, Any], game_id: str) -> Dict[str, Any]:
        """从 binding.list 里找指定游戏的绑定（appCode: arknights / endfield）。"""
        wanted = {"arknights": "arknights", "endfield": "endfield"}[game_id]
        entries = players.get("list")
        if not isinstance(entries, list):
            raise SklandQueryError("binding 返回缺少 list 数组")
        for app in entries:
            if isinstance(app, dict) and app.get("appCode") == wanted:
                bindings = app.get("bindingList")
                if isinstance(bindings, list) and bindings:
                    return bindings[0]
                raise GameNotBoundError("账号下没有这款游戏的角色，建号后自动生效")
        raise GameNotBoundError("账号下没有这款游戏的角色，建号后自动生效")


def _require_int_field(data: Dict[str, Any], key: str, label: str) -> int:
    """取整数字段，缺失/非数字一律按字段异常报错（不编造默认值）。"""
    value = data.get(key)
    if value is None:
        raise SklandQueryError("森空岛返回的数据不完整，可能需要更新软件版本")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SklandQueryError("森空岛返回的数据格式有变化，可能需要更新软件版本") from exc


def require_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value) == "":
        raise SklandQueryError("森空岛返回的数据不完整，可能需要更新软件版本")
    return str(value)


# --------------------------------------------------------------------------- #
# 注册表：provider -> game_id -> 查询器类
# --------------------------------------------------------------------------- #

_REGISTRY: Dict[str, Dict[str, Type[BaseQuerier]]] = {"mock": {}}


def register_querier(provider: str, game_id: str, querier_cls: Type[BaseQuerier]) -> None:
    """注册查询器，第 2 阶段接真实接口时调用它挂载。"""
    _REGISTRY.setdefault(provider, {})[game_id] = querier_cls


class UnknownProviderError(Exception):
    """配置里的 query.provider 没有对应实现。"""


#: 主 provider 未覆盖某游戏时的自动回退链：
#: miyoushe 不覆盖森空岛系 -> 这两款游戏自动挂到 skland 查询器。
_PROVIDER_FALLBACK: Dict[str, str] = {"arknights": "skland", "endfield": "skland"}


def get_querier(game_id: str, provider: str = "mock") -> BaseQuerier:
    """按 provider + game_id 取查询器实例。

    回退规则：
        mock                一律 MockQuerier
        已注册的 provider   注册表里查 game_id；查得到用真实查询器
        provider 已注册但该游戏没覆盖（如 miyoushe 下的明日方舟），
        先查 _PROVIDER_FALLBACK（森空岛系挂 skland），仍无则回退 MockQuerier 并打日志。
        provider 本身没注册（疑似拼错）仍抛 UnknownProviderError，不静默。
    """
    provider = (provider or "mock").lower()
    if provider == "mock":
        return MockQuerier()

    bucket = _REGISTRY.get(provider, {})
    querier_cls: Optional[Type[BaseQuerier]] = bucket.get(game_id) or bucket.get("*")
    if querier_cls is None:
        # 主 provider 未覆盖：尝试自动回退链（森空岛系 -> skland）
        fallback_provider = _PROVIDER_FALLBACK.get(game_id)
        if fallback_provider:
            fallback_bucket = _REGISTRY.get(fallback_provider, {})
            fallback_cls = fallback_bucket.get(game_id) or fallback_bucket.get("*")
            if fallback_cls is not None:
                logger.info(
                    "provider=%s 未覆盖游戏 %s，自动改用 %s 数据源",
                    provider, game_id, fallback_provider,
                )
                return fallback_cls()
        if bucket:
            logger.warning(
                "provider=%s 未覆盖游戏 %s，回退 mock 数据源（该游戏暂无真实数据源）",
                provider,
                game_id,
            )
            return MockQuerier()
        raise UnknownProviderError(
            f"provider=\"{provider}\" 尚未实现，请在 core/querier.py 中注册 {provider}/{game_id} 的查询器"
        )
    return querier_cls()


def supported_games(provider: str = "mock") -> List[str]:
    """当前 provider 能查询的游戏列表（mock 会返回内置全部游戏）。"""
    if provider == "mock":
        return list(MOCK_TASKS.keys())
    return list(_REGISTRY.get(provider, {}).keys())


__all__ = [
    "BaseQuerier",
    "MockQuerier",
    "MiyousheQuerier",
    "SklandQuerier",
    "UnknownProviderError",
    "get_querier",
    "register_querier",
    "supported_games",
    "MOCK_TASKS",
]


# --------------------------------------------------------------------------- #
# provider 注册（放在文件末尾，类定义之后）
# --------------------------------------------------------------------------- #

# mock：内置全部游戏
for _game_id in MOCK_TASKS:
    register_querier("mock", _game_id, MockQuerier)

# miyoushe：原神 / 崩铁 / 绝区零走真实接口；明日方舟、终末地未注册，
# get_querier 会经 _PROVIDER_FALLBACK 自动挂到 skland（见下）。
register_querier("miyoushe", "genshin", MiyousheQuerier)
register_querier("miyoushe", "starrail", MiyousheQuerier)
register_querier("miyoushe", "zzz", MiyousheQuerier)

# skland：明日方舟 / 终末地走真实接口
register_querier("skland", "arknights", SklandQuerier)
register_querier("skland", "endfield", SklandQuerier)
