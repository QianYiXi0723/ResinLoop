"""判断层（分析器）。

职责：把一串 GameStatus 汇总成一份 DailyReport —— 谁没做完、漏了哪几项、
谁查询失败，以及"到底要不要提醒"，并负责把结果渲染成
①控制台文本 ②Bark 推送的标题/正文 ③可序列化 dict（留给第 2 阶段存档用）。
不联网、不写文件，纯函数式，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .models import GameStatus

LINE = "-" * 52


@dataclass
class DailyReport:
    """一次检查的汇总结果。"""

    statuses: List[GameStatus] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)

    # ---------------- 分组 ---------------- #
    @property
    def unfinished(self) -> List[GameStatus]:
        """查询成功但仍有任务没做完的游戏。"""
        return [item for item in self.statuses if item.ok and not item.finished]

    @property
    def finished(self) -> List[GameStatus]:
        """全部日常已完成的游戏。"""
        return [item for item in self.statuses if item.ok and item.finished]

    @property
    def failed(self) -> List[GameStatus]:
        """查询失败的游戏。失败不算"未完成"，单独提示，避免误报催促。"""
        return [item for item in self.statuses if not item.ok]

    @property
    def unfinished_task_count(self) -> int:
        return sum(len(item.unfinished_tasks) for item in self.unfinished)

    @property
    def need_alert(self) -> bool:
        """是否需要提醒：只看"有未完成任务的游戏"。"""
        return bool(self.unfinished)

    # ---------------- 渲染：控制台 ---------------- #
    def to_console_text(self) -> str:
        lines: List[str] = []
        stamp = self.generated_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(LINE)
        lines.append(f"每日任务检查结果  {stamp}")
        lines.append(LINE)

        if not self.statuses:
            lines.append("没有需要检查的账号（accounts 全被禁用或为空）。")
            lines.append(LINE)
            return "\n".join(lines)

        for item in self.statuses:
            mark = "OK  " if item.ok and item.finished else ("ERR " if not item.ok else "--  ")
            lines.append(f"{mark}{item.summary_line()}")
            for task in item.unfinished_tasks:
                lines.append(f"      · 未完成：{task.describe()}")
            # 资源类数据（树脂/体力/电量/派遣）只展示，不参与判定、不触发推送
            if item.info_tasks:
                detail = "、".join(task.describe() for task in item.info_tasks)
                lines.append(f"      · 仅展示：{detail}")

        lines.append(LINE)
        total = len(self.statuses)
        lines.append(
            f"合计 {total} 个账号：已完成 {len(self.finished)}，"
            f"未完成 {len(self.unfinished)}（漏 {self.unfinished_task_count} 项），"
            f"查询失败 {len(self.failed)}"
        )

        if self.unfinished:
            names = "、".join(item.game_name for item in self.unfinished)
            lines.append(f"结论：还没做完 -> {names}")
        elif self.failed and not self.finished:
            lines.append("结论：全部查询失败，无法判断，建议检查凭证后重试")
        else:
            lines.append("结论：今天的日常都做完了，可以放心摆烂")
        lines.append(LINE)
        return "\n".join(lines)

    # ---------------- 渲染：推送 ---------------- #
    def to_push_payload(self) -> Tuple[str, str]:
        """生成 (标题, 正文)。标题短、正文按游戏分行，适配 Bark 的小卡片。"""
        total = len(self.statuses)
        title = f"游戏日常：{len(self.unfinished)}/{total} 个未完成"

        lines: List[str] = []
        lines.append(self.generated_at.strftime("%m-%d %H:%M"))
        for item in self.unfinished:
            tasks = "、".join(task.describe() for task in item.unfinished_tasks)
            lines.append(f"[未完成] {item.game_name}：{tasks}")

        if self.finished:
            names = "、".join(item.game_name for item in self.finished)
            lines.append(f"[已完成] {names}")

        if self.failed:
            names = "、".join(item.game_name for item in self.failed)
            lines.append(f"[查询失败] {names}")

        if not self.unfinished and not self.failed:
            lines.append("今天的日常全部完成，本条为测试推送。")

        lines.append(f"共漏 {self.unfinished_task_count} 项，别拖到 23:59。")
        return title, "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "need_alert": self.need_alert,
            "unfinished_task_count": self.unfinished_task_count,
            "summary": {
                "total": len(self.statuses),
                "finished": len(self.finished),
                "unfinished": len(self.unfinished),
                "failed": len(self.failed),
            },
            "games": [item.to_dict() for item in self.statuses],
        }


def build_report(statuses: List[GameStatus]) -> DailyReport:
    """入口函数：状态列表 -> 汇总报告。"""
    return DailyReport(statuses=list(statuses))
