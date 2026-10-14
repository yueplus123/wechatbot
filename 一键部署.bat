@echo off
cd /d "%~dp0"
title one-click deploy

REM Run on the cloud PC after unzipping. Safe to re-run.

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" (
  echo [FAIL] PowerShell not found. Use fix_pip.bat instead.
  pause
  exit /b 1
)
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
echo.
pause
