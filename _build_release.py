# -*- coding: utf-8 -*-
"""构建可外发的发布包：剔除全部本机凭据 / 缓存 / 日志，并生成通用启动脚本。

产物：
  release/gamedaily/          可直接发给别人的目录
  dist/游戏日常提醒_v1.zip

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
OUT = ROOT / "release" / "gamedaily"
ZIP_DIR = ROOT / "dist"
ZIP_NAME = "游戏日常提醒_v1"


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

# ------------------------------------------------------- 通用 setup_task.bat
SETUP_BAT = r"""@echo off
rem ============================================================
rem  Create a Windows Scheduled Task that runs run.bat daily 22:00
rem  Right-click - Run as administrator.
rem  Keep this file ASCII-only: cmd.exe encoding safety.
rem ============================================================
setlocal
chcp 65001 >nul 2>&1
set "TASKNAME=GameDailyCheck"
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "BAT=%DIR%\run.bat"

fltmc >nul 2>&1
if errorlevel 1 (
    echo [INFO] Administrator rights required, asking for elevation...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cls
echo ============================================================
echo   Create task "%TASKNAME%" : daily 22:00
echo   Script: %BAT%
echo ============================================================
echo.

if not exist "%BAT%" (
    echo [FAIL] run.bat not found next to this file.
    goto :end
)

echo [1/3] creating scheduled task ...
schtasks /Create /TN "%TASKNAME%" /TR "\"%BAT%\"" /SC DAILY /ST 22:00 /RL HIGHEST /F
if errorlevel 1 (
    echo [FAIL] schtasks returned an error. See message above.
    goto :end
)
echo   OK

echo.
echo [2/3] tuning settings ...
powershell -NoProfile -Command "$n='%TASKNAME%'; $t=Get-ScheduledTask -TaskName $n; $t.Settings.StartWhenAvailable=$true; $t.Settings.DisallowStartIfOnBatteries=$false; $t.Settings.StopIfGoingOnBatteries=$false; try{Set-ScheduledTask -TaskName $n -Settings $t.Settings -ErrorAction Stop; '  OK'}catch{'  [warn] '+$_.Exception.Message}"

echo.
echo [3/3] verifying ...
schtasks /Query /TN "%TASKNAME%" /FO LIST

echo.
echo ============================================================
echo   Done. The task will fire every day at 22:00.
echo   Run it once now:  schtasks /Run /TN "%TASKNAME%"
echo   Check log:        %DIR%\logs\cron_run.log
echo.
echo   To hide the console window, make it run whether the user
echo   is logged on or not:
echo       schtasks /Change /TN "%TASKNAME%" /RU %USERNAME% /RP *
echo   (type your Windows password when asked)
echo ============================================================

:end
echo.
pause
"""

# ---------------------------------------------------------------- 上手说明
README_TXT = """游戏日常任务提醒 —— 开始使用
================================================

这是一个本地小脚本：每天定时查一次你的游戏日常进度，
没做完就推送到手机（微信 / QQ）。

支持：原神、崩坏：星穹铁道、绝区零、明日方舟、明日方舟：终末地


一、准备
------------------------------------------------
1. 装 Python 3.10 或更高版本
   下载：https://www.python.org/downloads/
   安装时务必勾选  "Add python.exe to PATH"

2. 把本文件夹放到一个固定的位置，比如
       D:\\Tools\\gamedaily
   （路径里有空格没关系，但别放在压缩包里直接运行）


二、装依赖
------------------------------------------------
在本文件夹地址栏输入 cmd 回车，然后执行：

    pip install -r requirements.txt


三、填配置
------------------------------------------------
用记事本打开 config.json，至少改这两块：

1) 游戏账号凭据 —— 两种方式选一种

   方式 A（推荐，扫码登录，自动写配置）
       python main.py --login-mihoyo
       用手机米游社 App 扫码，三款米家游戏的凭据会自动写好。

       python main.py --login-skland
       用手机森空岛 App 扫码（鹰角的两款游戏）。

   方式 B（手动填）
       自己从浏览器 Cookie 里复制 ltuid / ltoken 填进 accounts。

2) 推送通道 —— 选一个填就行

   Server酱（微信，最简单）
       打开 https://sct.ftqq.com 微信扫码登录，
       复制 SendKey，填到 push.serverchan.sendkey，
       并把 "channels" 改成 ["serverchan"]。

   Qmsg酱（QQ）
       打开 https://qmsg.zendee.cn 登录并添加机器人好友，
       复制 key，填到 push.qmsg.key，
       并把 "channels" 改成 ["qmsg"]。

   想两个都收就写 ["serverchan", "qmsg"]，互不影响。


四、先手动跑一次
------------------------------------------------
    python main.py

没做完会立即推送一条；都做完了默认不打扰。
想强制发一条测试：python main.py --force-alert


五、设置每天定时
------------------------------------------------
右键"以管理员身份运行"  setup_task.bat

它会建一个名为 GameDailyCheck 的计划任务，每天 22:00 自动跑。
想改时间，重新运行它之前改一下里面的 /ST 22:00。

默认是"仅当用户登录时运行"，会闪一下黑窗口。
想完全后台静默，执行：

    schtasks /Change /TN "GameDailyCheck" /RU 你的用户名 /RP *

然后输入你的 Windows 登录密码。


六、排错
------------------------------------------------
- 日志文件：logs\\dailycheck.log（详细）、logs\\cron_run.log（定时任务输出）
- 查询报 1034 / 5003：米游社风控，换网络（手机热点）重试
- 提示"没有找到 Python"：重装 Python 并勾选 Add to PATH
- 详细说明见 README.md


七、说明
------------------------------------------------
- 本文件夹里的 config.json 是空白模板，需要你自己填。
- 不要把自己填好的 config.json 发给别人，里面有账号 Cookie。
- 脚本只读查询，不会代打游戏、不会做任何签到操作。
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
    for py in sorted((ROOT / "core").glob("*.py")):
        shutil.copy2(py, OUT / "core" / py.name)
    shutil.copy2(ROOT / "requirements.txt", OUT / "requirements.txt")
    shutil.copy2(ROOT / "README.md", OUT / "README.md")

    # 2) 空白配置（模板内容，绝无凭据）
    shutil.copy2(ROOT / "config.example.json", OUT / "config.json")
    shutil.copy2(ROOT / "config.example.json", OUT / "config.example.json")

    # 3) 通用启动 / 部署脚本
    write_crlf_ascii(OUT / "run.bat", RUN_BAT)
    write_crlf_ascii(OUT / "setup_task.bat", SETUP_BAT)
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
