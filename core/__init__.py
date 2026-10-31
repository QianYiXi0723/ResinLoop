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

#: 版本号唯一来源。界面左下角、打包脚本的 release 目录与 zip 名都从它派生，
#: 发版时只改这一处（shell/ResinLoop.csproj 的 <Version> 由打包脚本断言一致）。
__version__ = "2.3.0"
