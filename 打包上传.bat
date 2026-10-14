@echo off
cd /d "%~dp0"
title pack

REM Build ..\xinhuo-bot-deploy.zip (no .venv / logs / caches)
REM   this.bat        include database
REM   this.bat nodb   without database

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if /I "%~1"=="nodb" (
  "%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack.ps1" -NoDb
) else (
  "%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack.ps1"
)
echo.
pause
