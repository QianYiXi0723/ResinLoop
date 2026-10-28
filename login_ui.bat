@echo off
rem ============================================================
rem  Silently start the console server (pythonw, no console
rem  window) and open the page in your browser. This window
rem  exits immediately - nothing lingers in the taskbar.
rem  ASCII-only on purpose: cmd.exe encoding safety.
rem ============================================================
setlocal
chcp 65001 >nul 2>&1

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%" || goto :no_dir

set "PYW="
for %%v in (313 312 311 310 39) do (
    if not defined PYW (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\pythonw.exe" (
            set "PYW=%LOCALAPPDATA%\Programs\Python\Python%%v\pythonw.exe"
        )
    )
)
if not defined PYW goto :no_python

start "" "%PYW%" login_ui.py
exit /b 0

:no_dir
echo [FATAL] cannot enter %DIR%
pause
exit /b 1

:no_python
echo [FATAL] pythonw.exe not found. Install Python 3.10+.
pause
exit /b 1
