"""配置模块。

职责：把 config.json 变成强类型的 AppConfig 对象，并提供保存 / 加载 / 默认值
生成能力。所有字段缺失都走默认值，字段非法则抛 ConfigError（不静默吞掉），
保证下游模块拿到的配置一定可用。

覆盖内容：账号凭证、推送配置（本阶段仅 Bark）、每日检查时间、测试模式、日志与查询设置。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

TEST_MODE_AUTO = "auto"
TEST_MODE_FORCE_ALERT = "force_alert"
TEST_MODE_FORCE_SILENT = "force_silent"

VALID_TEST_MODES: Tuple[str, ...] = (
    TEST_MODE_AUTO,
    TEST_MODE_FORCE_ALERT,
    TEST_MODE_FORCE_SILENT,
)

TEST_MODE_LABELS: Dict[str, str] = {
    TEST_MODE_AUTO: "自动判断（有未完成项才推送）",
    TEST_MODE_FORCE_ALERT: "强制提醒（无论结果都推送）",
    TEST_MODE_FORCE_SILENT: "强制静默（只跑流程和日志，不推送）",
}

#: 内置游戏标识 -> 展示名。新增游戏只需在这里登记 + 在 querier 里注册查询器。
KNOWN_GAMES: Dict[str, str] = {
    "genshin": "原神",
    "starrail": "崩坏：星穹铁道",
    "zzz": "绝区零",
    "arknights": "明日方舟",
    "endfield": "终末地",
}

#: 米游社数据源覆盖的游戏（query.mihoyo_enabled=False 时跳过它们）
MIHOYO_GAMES: Tuple[str, ...] = ("genshin", "starrail", "zzz")

#: 错过时间点后的补发窗口（分钟）。0 = 关掉补发，最大 720 分钟（12 小时）。
#: 例如 180：21:30 那轮因为休眠没跑到，只要在 23:59 前醒过来，就会立刻补跑一次。
CATCH_UP_MINUTES_DEFAULT = 180
CATCH_UP_MINUTES_MAX = 720

#: 体力溢出预警阈值：当前值 >= 阈值就强制推一条（0 = 关闭该游戏的预警）。
#: 判定完全复用定时检查那一次查询的结果，**不额外发任何请求**。
RESOURCE_THRESHOLD_DEFAULT: Dict[str, int] = {
    "genshin": 160,    # 原粹树脂，上限 200
    "starrail": 240,   # 开拓力，上限 240
    "zzz": 200,        # 电量，上限 240
    "arknights": 135,  # 理智，上限随账号等级（常见 135~180）
}

#: 各游戏"体力/树脂/电量/理智"在任务列表里的显示名，用于从查询结果里认出那一项。
#: 值与 core/querier.py 里 TaskItem 的 name 必须一致。
RESOURCE_TASK_NAMES: Dict[str, str] = {
    "genshin": "原粹树脂",
    "starrail": "开拓力",
    "zzz": "电量",
    "arknights": "理智",
}

#: 阈值允许的最大值（体力上限普遍在 200~240 之间，留点余量）
RESOURCE_THRESHOLD_MAX = 1000


class ConfigError(Exception):
    """配置文件缺失 / 字段非法时抛出，由 main 统一转成友好提示。"""


# --------------------------------------------------------------------------- #
# 默认配置（首次运行据此生成 config.json，也是配置示例的单一来源）
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "test_mode": TEST_MODE_AUTO,
        "check_times": ["08:30", "20:30"],
        "catch_up_minutes": CATCH_UP_MINUTES_DEFAULT,
        "genshin_resin_threshold": RESOURCE_THRESHOLD_DEFAULT["genshin"],
        "starrail_stamina_threshold": RESOURCE_THRESHOLD_DEFAULT["starrail"],
        "zzz_energy_threshold": RESOURCE_THRESHOLD_DEFAULT["zzz"],
        "arknights_ap_threshold": RESOURCE_THRESHOLD_DEFAULT["arknights"],
    },
    "accounts": [
        {
            "game": "genshin",
            "nickname": "旅行者",
            "enabled": True,
            "credential": {
                "uid": "100000000",
                "server": "cn_gf01",
                "cookie": "ltuid=请填写; ltoken=请填写",
            },
        },
        {
            "game": "starrail",
            "nickname": "开拓者",
            "enabled": True,
            "credential": {
                "uid": "100000000",
                "server": "prod_gf_cn",
                "cookie": "ltuid=请填写; ltoken=请填写",
            },
        },
        {
            "game": "zzz",
            "nickname": "绳匠",
            "enabled": True,
            "credential": {
                "uid": "100000000",
                "server": "prod_gf_cn",
                "cookie": "ltuid=请填写; ltoken=请填写",
            },
        },
        {
            "game": "arknights",
            "nickname": "博士",
            "enabled": True,
            "credential": {
                # 鹰角通行证 Token：脚本会自动换取森空岛 cred 并缓存
                "token": "请填写鹰角通行证Token",
            },
        },
        {
            "game": "endfield",
            "nickname": "管理员",
            "enabled": True,
            "credential": {
                "token": "请填写鹰角通行证Token",
            },
        },
    ],
    "push": {
        "channels": ["bark"],
        "always_notify": False,
        "bark": {
            "enabled": True,
            "server": "https://api.day.app",
            "device_key": "",
            "group": "游戏日常",
            "sound": "birdsong",
            "icon": "",
            "level": "active",
            "timeout": 10,
        },
        "serverchan": {
            "enabled": True,
            "sendkey": "",
            "timeout": 10,
        },
        "qmsg": {
            "enabled": True,
            "key": "",
            "server": "https://qmsg.zendee.cn",
            "timeout": 10,
        },
        "feishu": {
            "enabled": True,
            "webhook": "",
            "secret": "",
            "timeout": 10,
        },
        "dingtalk": {
            "enabled": True,
            "webhook": "",
            "secret": "",
            "timeout": 10,
        },
    },
    "query": {
        "provider": "mock",
        "timeout": 10,
        "mihoyo_enabled": True,
        "proxy": "",
        "mihoyo_device_mode": "auto",
        "mihoyo_task_scope": "daily",
    },
    "logging": {
        "dir": "logs",
        "level": "INFO",
        "keep_days": 14,
    },
}


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #


@dataclass
class AccountConfig:
    """一个游戏账号的配置。credential 本阶段不参与真实请求，仅做结构占位。"""

    game: str
    nickname: str = ""
    enabled: bool = True
    credential: Dict[str, str] = field(default_factory=dict)

    @property
    def game_name(self) -> str:
        return KNOWN_GAMES.get(self.game, self.game)

    @classmethod
    def from_dict(cls, data: Any, index: int) -> "AccountConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"accounts[{index}] 必须是 JSON 对象")
        game = str(data.get("game", "")).strip()
        if not game:
            raise ConfigError(f"accounts[{index}] 缺少 game 字段")
        credential = data.get("credential") or {}
        if not isinstance(credential, dict):
            raise ConfigError(f"accounts[{index}].credential 必须是 JSON 对象")
        return cls(
            game=game,
            nickname=str(data.get("nickname", "")),
            enabled=bool(data.get("enabled", True)),
            credential={str(k): str(v) for k, v in credential.items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game": self.game,
            "nickname": self.nickname,
            "enabled": self.enabled,
            "credential": dict(self.credential),
        }


@dataclass
class BarkConfig:
    """Bark 推送配置。device_key 为空时视为"未配置"，推送阶段优雅跳过。"""

    enabled: bool = True
    server: str = "https://api.day.app"
    device_key: str = ""
    group: str = "游戏日常"
    sound: str = "birdsong"
    icon: str = ""
    level: str = "active"
    timeout: int = 10

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.device_key.strip())

    @classmethod
    def from_dict(cls, data: Any) -> "BarkConfig":
        if data is None:
            return cls(enabled=False)
        if not isinstance(data, dict):
            raise ConfigError("push.bark 必须是 JSON 对象")
        return cls(
            enabled=bool(data.get("enabled", True)),
            server=str(data.get("server") or "https://api.day.app").rstrip("/"),
            device_key=str(data.get("device_key") or "").strip(),
            group=str(data.get("group") or "游戏日常"),
            sound=str(data.get("sound") or ""),
            icon=str(data.get("icon") or ""),
            level=str(data.get("level") or "active"),
            timeout=int(data.get("timeout") or 10),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "server": self.server,
            "device_key": self.device_key,
            "group": self.group,
            "sound": self.sound,
            "icon": self.icon,
            "level": self.level,
            "timeout": self.timeout,
        }


@dataclass
class ServerChanConfig:
    """Server 酱（sctapi.ftqq.com）推送配置。sendkey 为空时视为"未配置"。"""

    enabled: bool = True
    sendkey: str = ""
    timeout: int = 10

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.sendkey.strip())

    @classmethod
    def from_dict(cls, data: Any) -> "ServerChanConfig":
        if data is None:
            return cls(enabled=False)
        if not isinstance(data, dict):
            raise ConfigError("push.serverchan 必须是 JSON 对象")
        return cls(
            enabled=bool(data.get("enabled", True)),
            sendkey=str(data.get("sendkey") or "").strip(),
            timeout=int(data.get("timeout") or 10),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "sendkey": self.sendkey, "timeout": self.timeout}


@dataclass
class QmsgConfig:
    """Qmsg 酱（qmsg.zendee.cn）推送配置：把消息发到 QQ。

    接口：GET {server}/send/{key}?msg=<内容>（旧版口子，改版后另有 /v3/send）。
    成功判定：返回 JSON `success == true`（旧版同时带 `code == 0`）。

    key 为 Qmsg 控制台里那串 Key；server 一般不用改，留空则用官方地址。
    """

    enabled: bool = True
    key: str = ""
    server: str = "https://qmsg.zendee.cn"
    timeout: int = 10

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.key.strip())

    @classmethod
    def from_dict(cls, data: Any) -> "QmsgConfig":
        if data is None:
            return cls(enabled=False)
        if not isinstance(data, dict):
            raise ConfigError("push.qmsg 必须是 JSON 对象")
        return cls(
            enabled=bool(data.get("enabled", True)),
            key=str(data.get("key") or "").strip(),
            server=str(data.get("server") or "https://qmsg.zendee.cn").strip().rstrip("/"),
            timeout=int(data.get("timeout") or 10),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "key": self.key,
            "server": self.server,
            "timeout": self.timeout,
        }


@dataclass
class FeishuConfig:
    """飞书自定义机器人 webhook 推送配置。secret 为加签密钥（可选）。"""

    enabled: bool = True
    webhook: str = ""
    secret: str = ""
    timeout: int = 10

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.webhook.strip())

    @classmethod
    def from_dict(cls, data: Any) -> "FeishuConfig":
        if data is None:
            return cls(enabled=False)
        if not isinstance(data, dict):
            raise ConfigError("push.feishu 必须是 JSON 对象")
        return cls(
            enabled=bool(data.get("enabled", True)),
            webhook=str(data.get("webhook") or "").strip(),
            secret=str(data.get("secret") or "").strip(),
            timeout=int(data.get("timeout") or 10),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "webhook": self.webhook,
            "secret": self.secret,
            "timeout": self.timeout,
        }


@dataclass
class DingTalkConfig:
    """钉钉自定义机器人 webhook 推送配置。secret 为加签密钥（可选）。"""

    enabled: bool = True
    webhook: str = ""
    secret: str = ""
    timeout: int = 10

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.webhook.strip())

    @classmethod
    def from_dict(cls, data: Any) -> "DingTalkConfig":
        if data is None:
            return cls(enabled=False)
        if not isinstance(data, dict):
            raise ConfigError("push.dingtalk 必须是 JSON 对象")
        return cls(
            enabled=bool(data.get("enabled", True)),
            webhook=str(data.get("webhook") or "").strip(),
            secret=str(data.get("secret") or "").strip(),
            timeout=int(data.get("timeout") or 10),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "webhook": self.webhook,
            "secret": self.secret,
            "timeout": self.timeout,
        }


@dataclass
class PushConfig:
    """推送配置容器。按 push.channels 决定实际启用哪几个通道。"""

    channels: List[str] = field(default_factory=lambda: ["bark"])
    #: 全部日常已完成时是否也发一条「今日全部完成」的平安通知
    always_notify: bool = False
    bark: BarkConfig = field(default_factory=BarkConfig)
    serverchan: ServerChanConfig = field(default_factory=ServerChanConfig)
    qmsg: QmsgConfig = field(default_factory=QmsgConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    dingtalk: DingTalkConfig = field(default_factory=DingTalkConfig)

    @classmethod
    def from_dict(cls, data: Any) -> "PushConfig":
        if data is None:
            return cls(channels=[], bark=BarkConfig(enabled=False),
                       serverchan=ServerChanConfig(enabled=False),
                       qmsg=QmsgConfig(enabled=False),
                       feishu=FeishuConfig(enabled=False),
                       dingtalk=DingTalkConfig(enabled=False))
        if not isinstance(data, dict):
            raise ConfigError("push 必须是 JSON 对象")
        channels = data.get("channels") or ["bark"]
        if not isinstance(channels, list):
            raise ConfigError("push.channels 必须是数组")
        # 兼容两种写法：push.qmsg.key（推荐）与平铺的 push.qmsg_key
        qmsg_data = data.get("qmsg")
        if qmsg_data is None and str(data.get("qmsg_key") or "").strip():
            qmsg_data = {"key": data.get("qmsg_key")}
        return cls(
            channels=[str(item).strip().lower() for item in channels if str(item).strip()],
            always_notify=bool(data.get("always_notify", False)),
            bark=BarkConfig.from_dict(data.get("bark")),
            serverchan=ServerChanConfig.from_dict(data.get("serverchan")),
            qmsg=QmsgConfig.from_dict(qmsg_data),
            feishu=FeishuConfig.from_dict(data.get("feishu")),
            dingtalk=DingTalkConfig.from_dict(data.get("dingtalk")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channels": list(self.channels),
            "always_notify": bool(self.always_notify),
            "bark": self.bark.to_dict(),
            "serverchan": self.serverchan.to_dict(),
            "qmsg": self.qmsg.to_dict(),
            "feishu": self.feishu.to_dict(),
            "dingtalk": self.dingtalk.to_dict(),
        }


@dataclass
class QueryConfig:
    """查询层配置。provider=mock 时全部走写死的模拟数据。

    mihoyo_enabled=False 时跳过全部米游社游戏（原神/崩铁/绝区零），
    仅保留森空岛系（明日方舟/终末地），用于米游社持续风控时临时止血。

    proxy:
        米游社请求（getFp / deviceLogin / saveDevice / dailyNote）统一走的代理，
        例如 "http://127.0.0.1:7890"。留空则直连（或沿用系统 HTTP(S)_PROXY）。
        因为 getFp 是按出口 IP 签发指纹的，换 IP 必须让全部请求一起换。

    mihoyo_device_mode:
        - "auto"（默认）：先按 GsCore 原生方式请求（带持久化设备指纹），
          命中 5003/10041/1034/10035 时追加 x-rpc-challenge-* 头重试一次。
        - "fp"：始终带完整设备头（device_id + device_fp + name/model）。
        - "gs"：完全不参与设备注册，只发基础头（最小化指纹暴露，排障用）。

    mihoyo_task_scope:
        - "daily"（默认）：只把「每日委托 / 每日实训 / 每日活跃度」算进完成度；
          树脂、开拓力、电量、探索派遣等资源数据只在控制台"仅展示"，不进未完成、
          不触发推送。
        - "full"：资源类也算完成度（树脂满/体力满/无进行中派遣才算完成）。
    """

    provider: str = "mock"
    timeout: int = 10
    mihoyo_enabled: bool = True
    proxy: str = ""
    mihoyo_device_mode: str = "auto"
    mihoyo_task_scope: str = "daily"

    #: 合法的设备模式取值（配置校验用）
    DEVICE_MODES = ("auto", "fp", "gs")
    #: 合法的任务口径
    TASK_SCOPES = ("daily", "full")

    @classmethod
    def from_dict(cls, data: Any) -> "QueryConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("query 必须是 JSON 对象")
        mode = str(data.get("mihoyo_device_mode") or "auto").strip().lower()
        if mode not in cls.DEVICE_MODES:
            raise ConfigError(
                f"query.mihoyo_device_mode 只能是 {'/'.join(cls.DEVICE_MODES)}，实际为 {mode!r}"
            )
        scope = str(data.get("mihoyo_task_scope") or "daily").strip().lower()
        if scope not in cls.TASK_SCOPES:
            raise ConfigError(
                f"query.mihoyo_task_scope 只能是 {'/'.join(cls.TASK_SCOPES)}，实际为 {scope!r}"
            )
        return cls(
            provider=str(data.get("provider") or "mock").strip().lower(),
            timeout=int(data.get("timeout") or 10),
            mihoyo_enabled=bool(data.get("mihoyo_enabled", True)),
            proxy=str(data.get("proxy") or "").strip(),
            mihoyo_device_mode=mode,
            mihoyo_task_scope=scope,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "timeout": self.timeout,
            "mihoyo_enabled": self.mihoyo_enabled,
            "proxy": self.proxy,
            "mihoyo_device_mode": self.mihoyo_device_mode,
            "mihoyo_task_scope": self.mihoyo_task_scope,
        }


@dataclass
class LoggingConfig:
    """日志配置。dir 为相对路径时，由 main 按项目根目录解析。"""

    dir: str = "logs"
    level: str = "INFO"
    keep_days: int = 14

    @classmethod
    def from_dict(cls, data: Any) -> "LoggingConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("logging 必须是 JSON 对象")
        return cls(
            dir=str(data.get("dir") or "logs"),
            level=str(data.get("level") or "INFO").upper(),
            keep_days=int(data.get("keep_days") or 14),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"dir": self.dir, "level": self.level, "keep_days": self.keep_days}


@dataclass
class AppConfig:
    """完整配置对象，main 只依赖它。"""

    test_mode: str = TEST_MODE_AUTO
    check_times: List[str] = field(default_factory=lambda: ["08:30", "20:30"])
    # 睡过头 / 关机错过时间点后，醒来立刻补跑一次；0 = 关闭补发
    catch_up_minutes: int = CATCH_UP_MINUTES_DEFAULT
    # 体力溢出预警：当前值 >= 阈值则强制推送（0 = 关闭）。只看已有查询结果，不额外请求。
    genshin_resin_threshold: int = RESOURCE_THRESHOLD_DEFAULT["genshin"]
    starrail_stamina_threshold: int = RESOURCE_THRESHOLD_DEFAULT["starrail"]
    zzz_energy_threshold: int = RESOURCE_THRESHOLD_DEFAULT["zzz"]
    arknights_ap_threshold: int = RESOURCE_THRESHOLD_DEFAULT["arknights"]
    accounts: List[AccountConfig] = field(default_factory=list)
    push: PushConfig = field(default_factory=PushConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    config_path: Optional[Path] = None

    @property
    def enabled_accounts(self) -> List[AccountConfig]:
        return [account for account in self.accounts if account.enabled]

    @property
    def disabled_accounts(self) -> List[AccountConfig]:
        """被 enabled=false 关掉的账号 —— 它们不会被查询，也不会进报表。"""
        return [account for account in self.accounts if not account.enabled]

    @property
    def enabled_game_ids(self) -> List[str]:
        """当前启用的游戏标识列表（保持配置里的顺序）。

        报表、日志、界面要列游戏时一律从这里取，**不要在别处写死游戏清单**。
        """
        return [account.game for account in self.accounts if account.enabled]

    @property
    def test_mode_label(self) -> str:
        return TEST_MODE_LABELS.get(self.test_mode, self.test_mode)

    @property
    def resource_thresholds(self) -> Dict[str, int]:
        """{game_id: 阈值}，交给 analyzer 判定体力溢出。"""
        return {
            "genshin": self.genshin_resin_threshold,
            "starrail": self.starrail_stamina_threshold,
            "zzz": self.zzz_energy_threshold,
            "arknights": self.arknights_ap_threshold,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app": {
                "test_mode": self.test_mode,
                "check_times": list(self.check_times),
                "catch_up_minutes": int(self.catch_up_minutes),
                "genshin_resin_threshold": int(self.genshin_resin_threshold),
                "starrail_stamina_threshold": int(self.starrail_stamina_threshold),
                "zzz_energy_threshold": int(self.zzz_energy_threshold),
                "arknights_ap_threshold": int(self.arknights_ap_threshold),
            },
            "accounts": [account.to_dict() for account in self.accounts],
            "push": self.push.to_dict(),
            "query": self.query.to_dict(),
            "logging": self.logging.to_dict(),
        }


# --------------------------------------------------------------------------- #
# 加载 / 保存
# --------------------------------------------------------------------------- #


def _parse_check_times(raw: Any) -> List[str]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("app.check_times 必须是非空数组，例如 [\"08:30\", \"20:30\"]")
    result: List[str] = []
    for item in raw:
        text = str(item).strip()
        if not TIME_PATTERN.match(text):
            raise ConfigError(f"app.check_times 中的 \"{text}\" 不是合法的 HH:MM 时间")
        if text not in result:
            result.append(text)
    return sorted(result)


def _parse_catch_up_minutes(raw: Any) -> int:
    """解析补发窗口（分钟）：留空/非法走默认值，0 或负数表示关闭补发。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return CATCH_UP_MINUTES_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"app.catch_up_minutes = {raw!r} 非法，请填 0（关闭）~ {CATCH_UP_MINUTES_MAX} 之间的整数"
        )
    if value < 0:
        return 0
    return min(value, CATCH_UP_MINUTES_MAX)


def _parse_threshold(raw: Any, label: str, game: str) -> int:
    """解析体力溢出阈值：留空 -> 该游戏的默认值；0 或负数 -> 关闭；非法 -> 报错。"""
    default = RESOURCE_THRESHOLD_DEFAULT.get(game, 0)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"app.{label} = {raw!r} 非法，请填 0（关闭）或正整数")
    if value < 0:
        return 0
    return min(value, RESOURCE_THRESHOLD_MAX)


def parse_config(data: Dict[str, Any], source: Optional[Path] = None) -> AppConfig:
    """把已解析的 dict 校验并转换成 AppConfig。缺省字段用默认值补齐。"""
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")

    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    app_section = merged.get("app") or {}
    if not isinstance(app_section, dict):
        raise ConfigError("app 必须是 JSON 对象")

    test_mode = str(app_section.get("test_mode") or TEST_MODE_AUTO).strip().lower()
    if test_mode not in VALID_TEST_MODES:
        raise ConfigError(
            f"app.test_mode = \"{test_mode}\" 非法，可选值：{' / '.join(VALID_TEST_MODES)}"
        )

    accounts_raw = merged.get("accounts") or []
    if not isinstance(accounts_raw, list):
        raise ConfigError("accounts 必须是数组")
    accounts = [AccountConfig.from_dict(item, i) for i, item in enumerate(accounts_raw)]

    return AppConfig(
        test_mode=test_mode,
        check_times=_parse_check_times(app_section.get("check_times")),
        catch_up_minutes=_parse_catch_up_minutes(app_section.get("catch_up_minutes")),
        genshin_resin_threshold=_parse_threshold(
            app_section.get("genshin_resin_threshold"), "genshin_resin_threshold", "genshin"),
        starrail_stamina_threshold=_parse_threshold(
            app_section.get("starrail_stamina_threshold"), "starrail_stamina_threshold", "starrail"),
        zzz_energy_threshold=_parse_threshold(
            app_section.get("zzz_energy_threshold"), "zzz_energy_threshold", "zzz"),
        arknights_ap_threshold=_parse_threshold(
            app_section.get("arknights_ap_threshold"), "arknights_ap_threshold", "arknights"),
        accounts=accounts,
        push=PushConfig.from_dict(merged.get("push")),
        query=QueryConfig.from_dict(merged.get("query")),
        logging=LoggingConfig.from_dict(merged.get("logging")),
        config_path=source,
    )


def load_config(path: str | Path, auto_create: bool = False) -> AppConfig:
    """读取本地 JSON 配置。

    Args:
        path: 配置文件路径。
        auto_create: 文件不存在时是否用默认配置生成一份（首次运行更顺手）。

    Raises:
        ConfigError: 文件不存在（且未 auto_create）、JSON 语法错误、字段非法。
    """
    config_path = Path(path).expanduser()
    if not config_path.exists():
        if auto_create:
            create_default_config(config_path)
        else:
            raise ConfigError(
                f"找不到配置文件：{config_path}\n"
                f"可先执行 `python main.py --init` 生成一份默认配置。"
            )

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败：{exc}") from exc

    if not raw_text.strip():
        raise ConfigError(f"配置文件为空：{config_path}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc

    return parse_config(data, source=config_path)


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    """把配置对象写回 JSON（UTF-8、缩进两格、保留中文），返回实际写入路径。"""
    target = Path(path or config.config_path or "config.json").expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    target.write_text(payload, encoding="utf-8")
    config.config_path = target
    return target


def create_default_config(path: str | Path) -> Path:
    """用内置 DEFAULT_CONFIG 生成一份可直接运行的配置，返回路径。"""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n"
    target.write_text(payload, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# 检查时间工具（本阶段只用于展示"下次检查"，不做常驻调度）
# --------------------------------------------------------------------------- #


def next_check_time(check_times: List[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """返回下一个检查时间点；当天已全部过点则返回明天的第一个点。"""
    if not check_times:
        return None
    current = now or datetime.now()
    for text in sorted(check_times):
        hour, minute = (int(part) for part in text.split(":"))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > current:
            return candidate
    hour, minute = (int(part) for part in sorted(check_times)[0].split(":"))
    tomorrow = current + timedelta(days=1)
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
