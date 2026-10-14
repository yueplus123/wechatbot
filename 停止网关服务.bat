@echo off
chcp 65001 >nul
cd /d "%~dp0"
title stop gateway services

echo Stopping Django / Worker ...
taskkill /F /FI "WINDOWTITLE eq xinhuo-Django*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq xinhuo-Worker*" >nul 2>&1

REM fallback: kill processes matching our commands (careful on shared machines)
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH 2^>nul') do (
  rem leave running; user can close window manually if needed
)

echo Done. If windows remain, close "xinhuo-Django" / "xinhuo-Worker" manually.
pause
