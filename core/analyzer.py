"""判断层（分析器）。

职责：把一串 GameStatus 汇总成一份 DailyReport —— 谁没做完、漏了哪几项、
谁查询失败，以及"到底要不要提醒"，并负责把结果渲染成
①控制台文本 ②Bark 推送的标题/正文 ③可序列化 dict（留给第 2 阶段存档用）。
不联网、不写文件，纯函数式，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import GameStatus, ResourceAlert

LINE = "-" * 52


@dataclass
class DailyReport:
    """一次检查的汇总结果。"""

    statuses: List[GameStatus] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)
    #: 体力 / 树脂 / 电量溢出预警（阈值命中才非空）
    resource_alerts: List[ResourceAlert] = field(default_factory=list)
    #: 活动结束提醒（剩 <=72 小时且今天还没提醒过的活动）
    activity_alerts: List[Any] = field(default_factory=list)

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

    @property
    def has_overflow(self) -> bool:
        """是否有体力/树脂/电量达到预警阈值。"""
        return bool(self.resource_alerts)

    @property
    def has_activity_alerts(self) -> bool:
        """是否有活动即将结束。"""
        return bool(self.activity_alerts)

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

        for alert in self.resource_alerts:
            lines.append(f"⚠️  {alert.warn_line()}")
        for alert in self.activity_alerts:
            lines.append(alert.warn_line())

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
        all_done = not self.unfinished and not self.failed

        # 标题一律用「已完成」做进度分子：查询失败的游戏仍占分母，但不会让标题
        # 出现「1/5 个未完成」却和正文「已完成 2 款」对不上的观感。
        if all_done:
            title = f"游戏日常：{total}/{total} 全部完成"
        else:
            title = f"游戏日常：{len(self.finished)}/{total} 已完成"
        if self.resource_alerts:
            title += f" · ⚠️ 体力将溢出（{len(self.resource_alerts)}）"
        if self.activity_alerts:
            title += f" · 📅 活动将结束（{len(self.activity_alerts)}）"

        lines: List[str] = []
        lines.append(self.generated_at.strftime("%m-%d %H:%M"))
        # 溢出预警放最上面：这条最急，别被下面的完成清单盖住
        for alert in self.resource_alerts:
            lines.append(alert.warn_line())
        for item in self.unfinished:
            tasks = "、".join(task.describe() for task in item.unfinished_tasks)
            lines.append(f"[未完成] {item.game_name}：{tasks}")

        if self.finished:
            names = "、".join(item.game_name for item in self.finished)
            lines.append(f"[已完成] {names}")

        if self.failed:
            names = "、".join(item.game_name for item in self.failed)
            lines.append(f"[查询失败] {names}")

        if self.resource_alerts:
            lines.append("先去把体力花掉，别浪费了。")
        elif all_done:
            lines.append("今天的日常全部完成，可以放心摆烂了。")
        else:
            lines.append(f"共漏 {self.unfinished_task_count} 项，别拖到 23:59。")

        # 活动提醒放在消息末尾：不打断"日常做没做完"这个主信息
        if self.activity_alerts:
            lines.append("")
            for alert in self.activity_alerts:
                lines.append(alert.warn_line())
        return title, "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "need_alert": self.need_alert,
            "has_overflow": self.has_overflow,
            "resource_alerts": [alert.to_dict() for alert in self.resource_alerts],
            "activity_alerts": [
                {
                    "game": alert.game_id,
                    "game_name": alert.game_name,
                    "title": alert.title,
                    "end": alert.end.strftime("%Y-%m-%d %H:%M:%S"),
                    "days_left": alert.days_left,
                }
                for alert in self.activity_alerts
            ],
            "unfinished_task_count": self.unfinished_task_count,
            "summary": {
                "total": len(self.statuses),
                "finished": len(self.finished),
                "unfinished": len(self.unfinished),
                "failed": len(self.failed),
            },
            "games": [item.to_dict() for item in self.statuses],
        }


def _parse_progress(progress: str) -> Optional[Tuple[int, int]]:
    """把 "160/200" 解析成 (160, 200)；格式不对返回 None。"""
    text = (progress or "").strip()
    if "/" not in text:
        return None
    left, _, right = text.partition("/")
    try:
        return int(left.strip()), int(right.strip())
    except ValueError:
        return None


def detect_resource_alerts(
    statuses: List[GameStatus],
    thresholds: Optional[Dict[str, int]] = None,
    *,
    task_names: Optional[Dict[str, str]] = None,
) -> List[ResourceAlert]:
    """从**已有的查询结果**里找达到阈值的体力 / 树脂 / 电量。

    这里不发任何请求——数值本来就在实时便笺里，只是以前只用于"仅展示"。
    阈值 <= 0 表示该游戏关闭预警。
    """
    from .config import RESOURCE_TASK_NAMES

    names = task_names or RESOURCE_TASK_NAMES
    table = thresholds or {}
    alerts: List[ResourceAlert] = []

    for status in statuses:
        if not status.ok:
            continue
        threshold = int(table.get(status.game_id) or 0)
        if threshold <= 0:
            continue
        want = names.get(status.game_id)
        if not want:
            continue
        for task in status.tasks:
            if task.name != want:
                continue
            parsed = _parse_progress(task.progress)
            if parsed is None:
                continue
            current, maximum = parsed
            if current >= threshold:
                alerts.append(ResourceAlert(
                    game_id=status.game_id,
                    game_name=status.game_name,
                    nickname=status.nickname,
                    resource=task.name,
                    current=current,
                    maximum=maximum,
                    threshold=threshold,
                ))
            break
    return alerts


def build_report(
    statuses: List[GameStatus],
    thresholds: Optional[Dict[str, int]] = None,
    activity_alerts: Optional[List[Any]] = None,
) -> DailyReport:
    """入口函数：状态列表 -> 汇总报告。

    ``thresholds`` 为 ``{game_id: 阈值}``（见 ``AppConfig.resource_thresholds``）；
    传了就顺带判定体力溢出，不传则不做（向后兼容旧调用）。
    ``activity_alerts`` 由 ``core.activities.pending_alerts()`` 提供，直接挂进报告。
    """
    return DailyReport(
        statuses=list(statuses),
        resource_alerts=detect_resource_alerts(statuses, thresholds),
        activity_alerts=list(activity_alerts or []),
    )
