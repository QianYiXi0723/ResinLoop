@echo off
rem ============================================================
rem  Open the graphical login page (login_ui.py) in your browser
rem  Double-click this file. Keep this window open while using it.
rem  ASCII-only on purpose: cmd.exe encoding safety.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%" || goto :no_dir

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
if not defined PYTHON_EXE goto :no_python

"%PYTHON_EXE%" login_ui.py
echo.
echo Login window closed.
pause
exit /b 0

:no_dir
echo [FATAL] cannot enter %DIR%
pause
exit /b 1

:no_python
echo [FATAL] Python not found. Install Python 3.10+ and tick "Add python.exe to PATH".
pause
exit /b 1
