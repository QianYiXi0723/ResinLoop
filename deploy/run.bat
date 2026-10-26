@echo off
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
