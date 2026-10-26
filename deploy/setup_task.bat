@echo off
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
