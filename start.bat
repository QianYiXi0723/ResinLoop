@echo off
rem ============================================================
rem  ResinLoop launcher (source mode) - zero lingering console.
rem
rem  What changed vs the old version, and why:
rem   * The old file relaunched itself as `start "" /min "%~f0" quiet`
rem     and then waited with two `timeout` calls. That minimized
rem     window sat on screen for ~5 seconds every launch, and could
rem     hang forever when stdin was redirected (non-interactive
rem     console / shortcut), which is the "black window never
rem     closes" report. Both are gone: no self-relaunch, no timeout.
rem   * Every child is started with pythonw.exe, never python.exe,
rem     so no console is attached to daemon.py / app.py. If
rem     pythonw.exe is missing we now stop with a clear error
rem     instead of silently falling back to the console interpreter.
rem   * The dependency check runs at most once: the result is cached
rem     in logs\.deps_ok, so normal launches exit in milliseconds.
rem     Delete that file to force a re-check (e.g. after edits to
rem     requirements.txt).
rem   * Keep this file ASCII-only (cmd.exe codepage safety).
rem ============================================================
setlocal
chcp 65001 >nul 2>&1
title ResinLoop

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%" || goto :no_dir

rem ---------- 1. locate python.exe ----------
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

rem ---------- 2. pythonw.exe (mandatory: no console for children) ----------
set "PYW="
for %%d in ("%PYEXE%") do set "PYW=%%~dpdpythonw.exe"
if not exist "%PYW%" goto :no_pythonw

if /i "%~1"=="check" (
    echo python  : %PYEXE%
    echo pythonw : %PYW%
    echo workdir : %DIR%
    echo marker  : %DIR%\logs\.deps_ok
    exit /b 0
)

rem ---------- 3. dependency check (cached, once) ----------
if not exist "logs" mkdir "logs"
if exist "logs\.deps_ok" goto :launch

echo [1/2] First run: checking dependencies...
"%PYEXE%" -c "import requests, qrcode, PIL, pystray, webview" >nul 2>&1
if errorlevel 1 goto :install
> "logs\.deps_ok" echo ok
goto :launch

:install
echo [1/2] Installing dependencies (first run only)...
"%PYEXE%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto :pip_fail
> "logs\.deps_ok" echo ok

rem ---------- 4. daemon + desktop window (both pythonw = no console) ----------
:launch
start "" "%PYW%" daemon.py
start "" "%PYW%" app.py
exit /b 0

:no_dir
echo [ERROR] cannot enter directory: %DIR%
pause
exit /b 1

:no_python
echo.
echo [ERROR] Python not found.
echo         Install Python 3.10+ and tick "Add python.exe to PATH".
echo         Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:no_pythonw
echo.
echo [ERROR] pythonw.exe not found next to:
echo         %PYEXE%
echo         Reinstall Python (pythonw.exe ships with it) or use dev.bat.
echo.
pause
exit /b 1

:pip_fail
echo.
echo [ERROR] Dependency installation failed.
echo         Run this manually to see the full output:
echo             "%PYEXE%" -m pip install -r requirements.txt
echo.
pause
exit /b 1
