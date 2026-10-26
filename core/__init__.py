"""游戏每日任务未完成提醒 —— 核心包。

分层：
    models    数据结构
    config    配置读写
    querier   数据查询（本阶段 mock）
    analyzer  汇总判断
    notifier  推送（本阶段 Bark）
    logger    日志初始化
"""

__all__ = ["models", "config", "querier", "analyzer", "notifier", "logger"]
__version__ = "0.1.0"
