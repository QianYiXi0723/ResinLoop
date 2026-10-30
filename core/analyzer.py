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

from .models import GameStatus, ResourceAlert, TaskItem

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
        """查询成功且有当日数据、但仍有任务没做完的游戏。

        数据待更新（stale）和"没有判定项"的都不算未完成 —— 前者没拿到今天的
        快照，后者本来就没有可判的东西，都不该进催促列表。
        """
        return [item for item in self.statuses if item.has_today_data and not item.finished]

    @property
    def finished(self) -> List[GameStatus]:
        """全部日常已完成的游戏。"""
        return [item for item in self.statuses if item.ok and item.finished]

    @property
    def failed(self) -> List[GameStatus]:
        """查询失败的游戏。失败不算"未完成"，单独提示，避免误报催促。"""
        return [item for item in self.statuses if not item.ok]

    @property
    def stale(self) -> List[GameStatus]:
        """数据待更新的游戏（如当天还没进过游戏，接口只给上次登录的快照）。

        既不是失败也不是未完成：不参与完成度判定、不触发催促，
        但要在推送/控制台里说明一句，免得"全部完成"把未知说成已完成。
        """
        return [item for item in self.statuses if item.ok and bool(item.stale)]

    @property
    def no_accounts(self) -> List[GameStatus]:
        """账号下没这款游戏角色的游戏（勾选并扫码了，但没玩过它）。

        凭证是有效的，所以也不算查询失败；等玩家建号后自动生效。
        """
        return [item for item in self.statuses if item.ok and bool(item.no_account)]

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
            if not item.ok:
                mark = "ERR "
            elif item.no_account:
                mark = "SKIP"
            elif item.stale:
                mark = "WAIT"
            elif item.finished:
                mark = "OK  "
            else:
                mark = "--  "
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
            + (f"，数据待更新 {len(self.stale)}" if self.stale else "")
            + (f"，未创建角色 {len(self.no_accounts)}" if self.no_accounts else "")
        )

        for alert in self.resource_alerts:
            lines.append(f"⚠️  {alert.warn_line()}")
        for alert in self.activity_alerts:
            lines.append(alert.warn_line())

        if self.unfinished:
            names = "、".join(item.game_name for item in self.unfinished)
            lines.append(f"结论：还没做完 -> {names}")
        elif self.failed and not self.finished and not self.stale and not self.no_accounts:
            lines.append("结论：全部查询失败，无法判断，建议检查凭证后重试")
        elif self.failed or self.stale or self.no_accounts:
            # 有查到并做完的，也有压根没拿到当日数据的 —— 别把未知说成已完成
            unknown = "、".join(
                item.game_name
                for item in (self.failed + self.stale + self.no_accounts)
            )
            lines.append(f"结论：{unknown} 没拿到当日数据，判断不了，别当成已经做完了")
        else:
            lines.append("结论：今天的日常都做完了，可以放心摆烂")
        lines.append(LINE)
        return "\n".join(lines)

    # ---------------- 渲染：推送 ---------------- #
    def to_push_payload(self) -> Tuple[str, str]:
        """生成 (标题, 正文)。标题短、正文按游戏分行，适配 Bark 的小卡片。"""
        total = len(self.statuses)
        # "全部完成"只在真的一款不剩时才说：数据待更新/未创建角色/查询失败
        # 的都算未知，否则会出现标题「5/5 全部完成」而方舟其实一整天没登录过的误导。
        all_done = not self.unfinished and not self.failed and not self.stale and not self.no_accounts

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

        if self.stale:
            # 一句话交代原因（"上次在线 xx"），不写接口细节
            for item in self.stale:
                lines.append(f"[数据待更新] {item.game_name}：{item.stale}")

        if self.no_accounts:
            names = "、".join(item.game_name for item in self.no_accounts)
            lines.append(f"[未创建角色] {names}：这个账号下还没有这些游戏，建号后自动生效")

        if self.resource_alerts:
            lines.append("先去把体力花掉，别浪费了。")
        elif all_done:
            lines.append("今天的日常全部完成，可以放心摆烂了。")
        elif not self.unfinished and (self.failed or self.stale or self.no_accounts):
            # 查成功的那几款都做完了，剩下的是没拿到当日数据的 —— 别再说「漏 0 项」
            if self.no_accounts and not self.failed and not self.stale:
                lines.append("没建号的几款不用管，在游戏里建号后自动生效。")
            elif self.stale:
                lines.append(
                    "数据待更新的几款进游戏刷新一次再看；"
                    "查询失败的先检查凭证或换个网络，别当成已经做完了。"
                )
            else:
                lines.append("没查到的几款先检查凭证或换个网络，别当成已经做完了。")
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
                "stale": len(self.stale),
                "no_account": len(self.no_accounts),
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


# --------------------------------------------------------------------------- #
# 推送预览（/api/push_preview）：快照重建 + 与实发共用 to_push_payload
#
# 页面「推送预览」卡的标题/正文一律出自本文件：正常态走 to_push_payload
# 同一条组装路径（页面所见 = 手机所得），空态也在这里结构化返回。
# tests/rl_test_push_preview.py 守卫「游戏日常：」不出现在前端源码里。
# --------------------------------------------------------------------------- #

WAITING_FIRST_CHECK_TITLE = "游戏日常：等待第一次巡检"
WAITING_FIRST_CHECK_BODY = (
    "还没有巡检数据。定时检查跑过一轮后，这里会按真实结果显示推送样式。"
)
NO_VALID_RECORDS_TITLE = "游戏日常：暂无有效记录"
NO_VALID_RECORDS_BODY = "今天还没有拿到任何有效的巡检数据。"


@dataclass
class _SnapshotAlert:
    """快照重建的活动提醒替身：to_push_payload 只调用 ``warn_line()``。

    快照里存的是 warn_line() 的当场输出（见 history.save_report_snapshot），
    这里原样吐回，保证隔天回看倒计时也逐字不漂。
    """

    warn_text: str = ""

    def warn_line(self) -> str:
        return self.warn_text


def report_from_snapshot(snap: Any) -> Optional[DailyReport]:
    """从 last_report.json 快照重建 DailyReport（结构不认识返回 None）。

    快照由 core.history.save_report_snapshot 在每轮巡检落盘时写入
    （GameStatus / ResourceAlert 的 to_dict 原样序列化）。重建出的对象喂给
    to_push_payload，与真实推送走完全相同的组装路径。
    """
    if not isinstance(snap, dict):
        return None
    statuses: List[GameStatus] = []
    for item in (snap.get("statuses") or []):
        if not isinstance(item, dict):
            continue
        tasks = [
            TaskItem(
                name=str(task.get("name") or ""),
                done=bool(task.get("done")),
                progress=str(task.get("progress") or ""),
                counted=bool(task.get("counted", True)),
                recover_seconds=(
                    task["recover_seconds"]
                    if isinstance(task.get("recover_seconds"), int) else None
                ),
            )
            for task in (item.get("tasks") or [])
            if isinstance(task, dict)
        ]
        statuses.append(GameStatus(
            game_id=str(item.get("game_id") or ""),
            game_name=str(item.get("game_name") or item.get("game_id") or ""),
            tasks=tasks,
            nickname=str(item.get("nickname") or ""),
            source=str(item.get("source") or "mock"),
            error=item.get("error") or None,
            stale=item.get("stale") or None,
            no_account=item.get("no_account") or None,
        ))
    if not statuses:
        return None

    resource_alerts = [
        ResourceAlert(
            game_id=str(a.get("game_id") or ""),
            game_name=str(a.get("game_name") or ""),
            nickname=str(a.get("nickname") or ""),
            resource=str(a.get("resource") or ""),
            current=int(a.get("current") or 0),
            maximum=int(a.get("maximum") or 0),
            threshold=int(a.get("threshold") or 0),
        )
        for a in (snap.get("resource_alerts") or [])
        if isinstance(a, dict)
    ]
    activity_alerts = [
        _SnapshotAlert(str(a.get("warn") or ""))
        for a in (snap.get("activity_alerts") or [])
        if isinstance(a, dict)
    ]
    generated_raw = str(snap.get("generated_at") or "")
    try:
        generated_at = (
            datetime.fromisoformat(generated_raw) if generated_raw else datetime.now()
        )
    except ValueError:
        generated_at = datetime.now()

    return DailyReport(
        statuses=statuses,
        generated_at=generated_at,
        resource_alerts=resource_alerts,
        activity_alerts=activity_alerts,
    )


def push_preview_from_snapshot(snap: Any) -> Dict[str, Any]:
    """/api/push_preview 的响应体：空态结构化，正常态与实发逐字同源。

    snap 为 None（没跑过巡检 / 快照缺失损坏）→ waiting_first_check；
    快照里没有任何当日有效数据（全失败/全 stale/全未建号）→ no_valid_records；
    其余 → to_push_payload 同一条组装路径（页面所见 = 手机所得）。
    dry-run 性质：不联网、不推送、不写任何文件。
    """
    report = report_from_snapshot(snap)
    if report is None:
        return {
            "state": "waiting_first_check",
            "title": WAITING_FIRST_CHECK_TITLE,
            "body": WAITING_FIRST_CHECK_BODY,
        }
    if not any(item.has_today_data for item in report.statuses):
        return {
            "state": "no_valid_records",
            "title": NO_VALID_RECORDS_TITLE,
            "body": NO_VALID_RECORDS_BODY,
        }
    title, body = report.to_push_payload()
    return {
        "state": "ok",
        "title": title,
        "body": body,
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
    }
