@echo off
chcp 65001 >nul
cd /d "%~dp0"
title setup_env

echo.
echo ============================================================
echo   Install Python + .venv (China mirrors)
echo ============================================================
echo.
pause

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"

REM Prefer PowerShell installer if available; otherwise use fix_pip.bat (CMD only)
if not exist "%PS%" goto fallback_pip
if not exist "%~dp0setup_env.ps1" goto fallback_pip
echo [..] run setup_env.ps1 ...
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_env.ps1"
set "RC=%ERRORLEVEL%"
goto after_setup

:fallback_pip

echo [WARN] PowerShell missing or setup_env.ps1 missing
echo [..] fallback to fix_pip.bat
if not exist "%~dp0fix_pip.bat" (
  echo [FAIL] fix_pip.bat not found
  pause
  exit /b 1
)
call "%~dp0fix_pip.bat"
set "RC=%ERRORLEVEL%"

:after_setup
echo.
if "%RC%"=="0" (
  echo [OK] Done.
  echo Next 1: double-click the start-gateway bat
  echo Next 2: open http://127.0.0.1:8000/panel/
) else (
  echo [FAIL] exit=%RC%
  echo Try: fix_pip.bat
  echo Log: data\remote_setup.log
)
echo.
pause
exit /b %RC%
