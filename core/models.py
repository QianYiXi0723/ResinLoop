"""数据模型层。

职责：定义跨模块复用的数据结构（任务项、游戏状态、推送结果），
只做数据承载与轻量派生属性，不含任何网络 / IO / 判断逻辑，
保证 querier、analyzer、notifier 三方对"一条游戏状态长什么样"理解一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskItem:
    """一条单个日常任务，例如"每日委托""消耗原粹树脂"。

    Attributes:
        name: 任务名称，展示用。
        done: 是否已完成。
        progress: 进度文本，如 "4/4"、"120/160"，无进度语义时留空。
        counted: 是否参与"完成度/未完成"判定。
            False 表示"仅展示"的资源类数据（树脂/体力/电量/派遣等），
            只在控制台打印，绝不进未完成列表、也不触发推送。
        recover_seconds: 距满秒数（仅资源类任务有值）。
            由查询器把接口的恢复字段（剩余秒数 / 满员时间戳）统一换算成
            "还差多少秒满"；API 未提供时为 None，前端走常数速率表估算。
    """

    name: str
    done: bool
    progress: str = ""
    counted: bool = True
    recover_seconds: Optional[int] = None

    @property
    def status_text(self) -> str:
        return "已完成" if self.done else "未完成"

    def describe(self) -> str:
        """带进度的短描述，用于拼提醒正文。"""
        if self.progress:
            return f"{self.name}({self.progress})"
        return self.name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "done": self.done,
            "progress": self.progress,
            "counted": self.counted,
            "recover_seconds": self.recover_seconds,
        }


@dataclass
class GameStatus:
    """一个账号在某款游戏里的当日日常状态。

    Attributes:
        game_id: 游戏标识，对应配置里的 accounts[].game。
        game_name: 游戏中文名，展示用。
        tasks: 任务明细；查询失败时为空列表。
        nickname: 账号昵称，展示用。
        source: 数据来源标记（mock / real），便于日志排查。
        error: 查询失败原因；为空表示查询成功。
    """

    game_id: str
    game_name: str
    tasks: List[TaskItem] = field(default_factory=list)
    nickname: str = ""
    source: str = "mock"
    error: Optional[str] = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.game_name

    @property
    def ok(self) -> bool:
        """是否查询成功。"""
        return self.error is None

    @property
    def counted_tasks(self) -> List[TaskItem]:
        """参与完成度判定的任务（排除"仅展示"的资源类数据）。"""
        return [task for task in self.tasks if task.counted]

    @property
    def info_tasks(self) -> List[TaskItem]:
        """仅展示、不参与判定的资源类数据（树脂/体力/电量/派遣等）。"""
        return [task for task in self.tasks if not task.counted]

    @property
    def finished(self) -> bool:
        """是否全部日常已完成。查询失败、无判定项一律视为"未完成/未知"。"""
        counted = self.counted_tasks
        if not self.ok or not counted:
            return False
        return all(task.done for task in counted)

    @property
    def unfinished_tasks(self) -> List[TaskItem]:
        """未完成的**判定项**；仅展示项永远不会出现在这里。"""
        return [task for task in self.counted_tasks if not task.done]

    @property
    def done_count(self) -> int:
        return sum(1 for task in self.counted_tasks if task.done)

    def summary_line(self) -> str:
        """单行摘要，用于控制台表格式输出。"""
        who = f"{self.game_name}（{self.nickname}）" if self.nickname else self.game_name
        if not self.ok:
            return f"{who}｜查询失败：{self.error}"
        counted = self.counted_tasks
        if not counted:
            return f"{who}｜无判定项（仅资源数据）｜0/0"
        head = "全部完成" if self.finished else f"未完成 {len(self.unfinished_tasks)} 项"
        return f"{who}｜{head}｜{self.done_count}/{len(counted)}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "game_name": self.game_name,
            "nickname": self.nickname,
            "source": self.source,
            "finished": self.finished,
            "error": self.error,
            "done_count": self.done_count,
            "total_count": len(self.counted_tasks),
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass
class ResourceAlert:
    """一条"体力要溢出了"的预警。

    只在定时检查顺带判定，**不产生任何额外请求**：数值来自本轮已经拿到的
    实时便笺（树脂 / 开拓力 / 电量），与阈值比一下即可。

    Attributes:
        game_id: 游戏标识。
        game_name: 游戏中文名。
        nickname: 账号昵称。
        resource: 资源显示名，如"原粹树脂"。
        current: 当前值。
        maximum: 上限值。
        threshold: 触发预警的阈值（>0）。
    """

    game_id: str
    game_name: str
    nickname: str
    resource: str
    current: int
    maximum: int
    threshold: int

    @property
    def who(self) -> str:
        return self.nickname or self.game_name

    def warn_line(self) -> str:
        """推送正文里那一行醒目警告。"""
        return (
            f"⚠️ 警告：{self.game_name}{self.resource}已达到 "
            f"{self.current}/{self.maximum}，即将溢出！"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "game_name": self.game_name,
            "nickname": self.nickname,
            "resource": self.resource,
            "current": self.current,
            "maximum": self.maximum,
            "threshold": self.threshold,
            "warn": self.warn_line(),
        }


@dataclass
class PushResult:
    """一次推送尝试的结果，用于日志与退出码判定。"""

    channel: str
    ok: bool
    message: str
    skipped: bool = False

    def __str__(self) -> str:
        if self.skipped:
            return f"[{self.channel}] 已跳过：{self.message}"
        flag = "成功" if self.ok else "失败"
        return f"[{self.channel}] {flag}：{self.message}"
