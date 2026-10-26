# -*- coding: utf-8 -*-
"""构建可外发的发布包：剔除全部本机凭据 / 缓存 / 日志，并生成通用启动脚本。

产物：
  release/resinloop/          可直接发给别人的目录
  dist/ResinLoop_v1.zip

注意：本文件会被提交到仓库，因此**不得硬编码任何真实 key**。
自检用的敏感值全部在运行时从本地 config.json / logs 里动态提取。
"""
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "release" / "resinloop"
ZIP_DIR = ROOT / "dist"
ZIP_NAME = "ResinLoop_v1"


def collect_secrets() -> List[str]:
    """从本地真实配置与缓存里提取敏感值，供打包自检使用。

    刻意不在源码里写死任何 key —— 否则这个脚本本身就成了泄漏源。
    config.json 不存在时（别人 clone 下来）返回空列表，自检自动跳过。
    """
    found: set[str] = set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if len(text) >= 8:
            found.add(text)

    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for node in (data.get("push") or {}).values():
            if isinstance(node, dict):
                for field in ("key", "sendkey", "device_key", "webhook", "secret"):
                    add(node.get(field))
        for account in data.get("accounts") or []:
            cred = account.get("credential") or {}
            add(cred.get("uid"))
            add(cred.get("token"))
            for part in str(cred.get("cookie") or "").replace(";", " ").split():
                if "=" in part:
                    add(part.split("=", 1)[1])

    def pick_field(path: Path, *fields: str) -> None:
        if not path.exists():
            return
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        stack = [blob]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in fields:
                        add(value)
                    stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)

    for cache in (ROOT / "logs").glob("skland_cred_*.json"):
        pick_field(cache, "cred", "token")
    # 只查 device_fp / device_id：这俩必须确保 logs 没被打包。
    # oaid 刻意不查 —— 它是 core/mihoyo_api.py 里的真机档案常量，
    # 参与设备指纹的缓存失效判断，改动会触发重新生成 device_fp（风控风险）。
    pick_field(ROOT / "logs" / "device_fp.json", "device_fp", "device_id")

    # mihoyo_binding.json 的键形如 "<ltuid>:genshin"，取冒号前那段
    binding = ROOT / "logs" / "mihoyo_binding.json"
    if binding.exists():
        try:
            for key in json.loads(binding.read_text(encoding="utf-8")):
                add(str(key).split(":", 1)[0])
        except (OSError, ValueError):
            pass

    return sorted(found)

# ---------------------------------------------------------------- 通用 run.bat
RUN_BAT = r"""@echo off
rem ============================================================
rem  Daily game check launcher (portable)
rem  Locates itself, finds Python, writes logs\cron_run.log
rem  Keep this file ASCII-only: cmd.exe encoding safety.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

rem --- project dir = folder of this .bat (strip trailing backslash) ---
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "LOG_FILE=%PROJECT_DIR%\logs\cron_run.log"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

cd /d "%PROJECT_DIR%" || goto :no_dir
if not exist "logs" mkdir "logs"

rem --- locate python: py launcher first, then well-known install dirs ---
set "PYTHON_EXE="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if not defined PYTHON_EXE (
    for %%v in (313 312 311 310 39) do (
        if not defined PYTHON_EXE (
            if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
                set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
            )
        )
    )
)
if not defined PYTHON_EXE (
    for %%d in ("C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310") do (
        if not defined PYTHON_EXE (
            if exist "%%~d\python.exe" set "PYTHON_EXE=%%~d\python.exe"
        )
    )
)
if not defined PYTHON_EXE goto :no_python

>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo ===== START %DATE% %TIME% =====
"%PYTHON_EXE%" main.py >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
>>"%LOG_FILE%" echo ===== END   %DATE% %TIME%  exit=%RC% =====
exit /b %RC%

:no_dir
echo [FATAL] cannot enter project dir: %PROJECT_DIR%
exit /b 1

:no_python
echo [FATAL] python.exe not found.
echo         Install Python 3.10+ and tick "Add python.exe to PATH".
>>"%LOG_FILE%" echo [FATAL] python.exe not found
exit /b 1
"""

# ---------------------------------------------------------------- 上手说明
README_TXT = """ResinLoop · 游戏日常巡查 —— 三步搞定
================================================

每天定时查一次游戏日常进度，没做完就推送到手机（微信 / QQ）。
支持：原神、崩坏：星穹铁道、绝区零、明日方舟、明日方舟：终末地


第零步：装 Python（只做一次）
------------------------------------------------
下载：https://www.python.org/downloads/
安装时务必勾选  "Add python.exe to PATH"

装完就不用管了，下面的依赖 start.bat 会自动装。


第一步：双击 start.bat
------------------------------------------------
它会自动做完这些事：

  1. 检查依赖，缺了自动 pip install
  2. 后台启动守护进程（右下角出现托盘图标）
  3. 弹出一个独立的控制台窗口（不是浏览器，是真正的应用窗口）

窗口标题是「ResinLoop · 游戏日常巡查」，关掉它不影响定时提醒。
那个黑框框显示"已启动"之后就可以关了。


第二步：在窗口里配三个页签
------------------------------------------------
【账号绑定】
  设备伪装（推荐，但不强制）
      手机扫页面上的二维码 -> 安装「设备信息」App
      -> 打开 App 点"复制" -> 粘到文本框 -> 点保存
      保存后必须重新扫码（设备身份变了）

  扫码登录
      米游社：勾几款就更新几款，扫 1 次码
      森空岛：勾几款就依次扫几次码

【通知绑定】
  Server酱（微信）：打开 https://sct.ftqq.com 微信扫码，
                    复制 SendKey 填进去，勾上"启用"
  Qmsg 酱（QQ） ：打开 https://qmsg.zendee.cn 登录，
                  复制 key 填进去，勾上"启用"
  填完点"发送测试推送"，手机收到就说明通了，再点"保存配置"。

【定时与状态】
  添加提醒时间点（例如 08:30、20:30）
  点"启动守护进程"，状态变成"运行中"就齐了。


第三步：挂机
------------------------------------------------
到点自动检查并推送，关掉网页不影响。

右下角托盘图标右键：
    打开控制台  /  立即执行一次  /  退出程序


常见问题
------------------------------------------------
- 扫码报 1034 / 5003（风控）
    换个网络（手机热点）重试；
    或在「账号绑定」贴上自己手机的设备指纹，通过率更高。

- 手机收不到提醒
    去「通知绑定」点"发送测试推送"，按页面提示排查。

- 想看详细日志
    logs\\dailycheck.log

- 想彻底停止
    网页「定时与状态」点"停止守护进程"，或托盘右键退出。

- 不要把自己填好的 config.json 发给别人，里面有账号 Cookie。
- 只读查询，不会代打游戏、不会自动签到。
- 本项目使用 MIT 许可证，可自由修改分发。
"""


def write_crlf_ascii(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))


def write_crlf_bom(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig"))


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "core").mkdir(parents=True)

    # 1) 源码与文档（README 里的真实指纹 / 出口 IP 就地脱敏，本地原件不动）
    shutil.copy2(ROOT / "main.py", OUT / "main.py")
    shutil.copy2(ROOT / "daemon.py", OUT / "daemon.py")
    shutil.copy2(ROOT / "app.py", OUT / "app.py")
    shutil.copy2(ROOT / "login_ui.py", OUT / "login_ui.py")
    shutil.copy2(ROOT / "login_ui.bat", OUT / "login_ui.bat")
    shutil.copy2(ROOT / "start.bat", OUT / "start.bat")
    shutil.copy2(ROOT / "LICENSE", OUT / "LICENSE")
    for py in sorted((ROOT / "core").glob("*.py")):
        shutil.copy2(py, OUT / "core" / py.name)
    shutil.copy2(ROOT / "requirements.txt", OUT / "requirements.txt")
    shutil.copy2(ROOT / "README.md", OUT / "README.md")

    # 1.1) tools/：手机端 APK（体积大，不进 git，但必须进发布包）
    tools_src = ROOT / "tools"
    if tools_src.is_dir():
        (OUT / "tools").mkdir(exist_ok=True)
        for item in sorted(tools_src.iterdir()):
            if item.is_file() and not item.name.startswith("."):
                shutil.copy2(item, OUT / "tools" / item.name)

    # 2) 空白配置（模板内容，绝无凭据）
    shutil.copy2(ROOT / "config.example.json", OUT / "config.json")
    shutil.copy2(ROOT / "config.example.json", OUT / "config.example.json")

    # 3) 通用启动脚本（start.bat 已是很随身的版本，直接复制；
    #    run.bat 用于手动跑一次检查，没有调度逻辑）
    write_crlf_ascii(OUT / "run.bat", RUN_BAT)
    write_crlf_bom(OUT / "开始使用.txt", README_TXT)

    # 4) 体检：敏感值从本地 config.json / logs 动态提取，源码里不存任何真实 key
    banned = collect_secrets()
    leaked = []
    for f in OUT.rglob("*"):
        if f.is_file():
            body = f.read_bytes().decode("utf-8", errors="ignore")
            for token in banned:
                if token in body:
                    leaked.append(f"{f.relative_to(OUT)} <- {token}")

    # 5) 打包
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = ZIP_DIR / ZIP_NAME
    archive = shutil.make_archive(str(zip_path), "zip", root_dir=OUT.parent, base_dir=OUT.name)

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"OUT_DIR   : {OUT}")
    print(f"FILES     : {sum(1 for f in OUT.rglob('*') if f.is_file())} 个, {total/1024:.0f} KB")
    print(f"ZIP       : {archive}  ({Path(archive).stat().st_size/1024:.0f} KB)")
    print(f"SECRETS   : {'干净' if not leaked else '发现泄漏 -> ' + str(leaked)}")
    print("--- 包内清单 ---")
    for f in sorted(OUT.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(OUT)}  ({f.stat().st_size}B)")


if __name__ == "__main__":
    main()
