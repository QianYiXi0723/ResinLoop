@echo off
rem ============================================================
rem  ResinLoop dev launcher - runs the WORKING TREE code with the
rem  system Python, and never leaves a black console window behind.
rem
rem  Usage (double-click = default mode):
rem    dev.bat            desktop window   (pythonw app.py)
rem    dev.bat web        browser mode     (pythonw login_ui.py)
rem    dev.bat daemon     daemon only      (pythonw daemon.py)
rem    dev.bat all        daemon + desktop window
rem    dev.bat exe        sync code into engine\ then start ResinLoop.exe
rem    dev.bat sync       sync code into engine\ only (no launch)
rem    dev.bat check      print resolved paths, launch nothing
rem
rem  Two layers, do not mix them up:
rem    ResinLoop.exe is only the C# shell. All business code lives in
rem    engine\ (portable python + *.py + ui\). So you do NOT need to
rem    pack a zip to test the exe: "dev.bat exe" copies the current
rem    working tree into engine\ and starts the shell.
rem    Only a change to shell\*.cs needs a real rebuild + ILRepack.
rem
rem  Why not start.bat:
rem    start.bat is the end-user entry: it runs a dependency check
rem    and always starts the daemon. dev.bat starts only what you
rem    ask for, so a scheduled check cannot fire a real push while
rem    you are debugging the UI.
rem
rem  Why there is no black window:
rem    every child is launched with pythonw.exe (GUI subsystem, no
rem    console). This batch has no self-relaunch, no `start /min`
rem    and no `timeout`, so it exits in milliseconds.
rem    Keep this file ASCII-only (cmd.exe codepage safety).
rem ============================================================
setlocal
chcp 65001 >nul 2>&1
title ResinLoop dev

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%" || goto :no_dir

rem ---------- locate pythonw.exe ----------
set "PYW="
for /f "delims=" %%i in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%i"
if not defined PYW (
    for %%v in (313 312 311 310 39) do (
        if not defined PYW (
            if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\pythonw.exe" (
                set "PYW=%LOCALAPPDATA%\Programs\Python\Python%%v\pythonw.exe"
            )
        )
    )
)
if not defined PYW goto :no_pythonw
if not exist "%PYW%" goto :no_pythonw

rem ---------- console interpreter (same folder as pythonw) ----------
set "PYEXE="
for %%d in ("%PYW%") do set "PYEXE=%%~dpdpython.exe"
if not exist "%PYEXE%" set "PYEXE=%PYW%"

if /i "%~1"=="check" (
    echo pythonw : %PYW%
    echo python  : %PYEXE%
    echo workdir : %DIR%
    echo arg     : [%~1]
    echo shell   : %DIR%\ResinLoop.exe
    echo engine  : %DIR%\engine\python\pythonw.exe
    exit /b 0
)

if not exist "app.py" goto :bad_tree

rem ---------- dispatch ----------
if /i "%~1"=="daemon" goto :do_daemon
if /i "%~1"=="web" goto :do_web
if /i "%~1"=="all" goto :do_all
if /i "%~1"=="sync" goto :do_sync
if /i "%~1"=="exe" goto :do_exe

rem default: desktop window only
start "" "%PYW%" app.py
exit /b 0

:do_daemon
start "" "%PYW%" daemon.py
exit /b 0

:do_web
start "" "%PYW%" login_ui.py
exit /b 0

:do_all
start "" "%PYW%" daemon.py
start "" "%PYW%" app.py
exit /b 0

:do_sync
"%PYEXE%" "_dev_sync_engine.py" %2
if errorlevel 1 goto :sync_fail
exit /b 0

:do_exe
if not exist "ResinLoop.exe" goto :no_exe
if not exist "engine\python\pythonw.exe" goto :no_engine
"%PYEXE%" "_dev_sync_engine.py"
if errorlevel 1 goto :sync_fail
start "" "%DIR%\ResinLoop.exe"
exit /b 0

:no_dir
echo [ERROR] cannot enter directory: %DIR%
pause
exit /b 1

:bad_tree
echo [ERROR] app.py not found in %DIR%
echo         run dev.bat from the ResinLoop source folder.
pause
exit /b 1

:no_exe
echo.
echo [ERROR] ResinLoop.exe not found in %DIR%
echo         Build the shell first: see shell\ (msbuild + ILRepack).
echo.
pause
exit /b 1

:no_engine
echo.
echo [ERROR] engine\python\pythonw.exe not found.
echo         The portable engine is missing: run _build_release.py once,
echo         or copy engine\ from release\ResinLoop_v2.0\.
echo.
pause
exit /b 1

:sync_fail
echo.
echo [ERROR] engine sync failed, see the message above.
echo.
pause
exit /b 1

:no_pythonw
echo.
echo [ERROR] pythonw.exe not found.
echo         Install Python 3.10+ and tick "Add python.exe to PATH".
echo         Download: https://www.python.org/downloads/
echo.
pause
exit /b 1
