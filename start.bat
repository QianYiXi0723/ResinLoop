@echo off
setlocal enabledelayedexpansion
chcp 936 >nul 2>&1
title 游戏日常提醒

rem ============================================================
rem  一键启动：守护进程 + 独立桌面窗口（不弹浏览器、不留黑框）
rem ============================================================

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%" || goto :no_dir

rem ---------- 1. 找到 Python ----------
set "PYEXE="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE (
    for %%v in (313 312 311 310 39) do (
        if not defined PYEXE (
            if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
                set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
            )
        )
    )
)
if not defined PYEXE goto :no_python

rem pythonw.exe：后台运行，不显示黑窗口
set "PYW="
for %%d in ("%PYEXE%") do set "PYW=%%~dpdpythonw.exe"
if not exist "%PYW%" set "PYW=%PYEXE%"

rem ---------- 2. 依赖自检 ----------
"%PYEXE%" -c "import requests, qrcode, PIL, pystray, webview" >nul 2>&1
if errorlevel 1 (
    echo [1/3] 首次运行，正在安装依赖，请稍候...
    "%PYEXE%" -m pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 goto :pip_fail
) else (
    echo [1/3] 依赖检查通过
)

rem ---------- 3. 先起守护进程（托盘图标、定时提醒） ----------
echo [2/3] 启动守护进程...
start "" "%PYW%" daemon.py

rem 给守护进程一点时间写 pid / 心跳，控制台一打开就能看到"运行中"
timeout /t 2 /nobreak >nul 2>&1

rem ---------- 4. 再起桌面窗口 ----------
echo [3/3] 启动控制台窗口...
start "" "%PYW%" app.py

echo.
echo 已启动。
echo   - 桌面窗口：游戏日常提醒控制台（关掉窗口不影响定时提醒）
echo   - 右下角托盘：右键可"打开控制台 / 立即执行一次 / 退出程序"
echo.
echo 本窗口可以关闭。
timeout /t 3 /nobreak >nul 2>&1
exit /b 0

:no_dir
echo [错误] 无法进入目录：%DIR%
pause
exit /b 1

:no_python
echo.
echo [错误] 没有找到 Python。
echo 请先安装 Python 3.10 或更高版本，安装时务必勾选 "Add python.exe to PATH"。
echo 下载地址：https://www.python.org/downloads/
echo.
pause
exit /b 1

:pip_fail
echo.
echo [错误] 依赖安装失败，请检查网络后重试。
echo 也可以在本目录手动执行：
echo     "%PYEXE%" -m pip install -r requirements.txt
pause
exit /b 1
