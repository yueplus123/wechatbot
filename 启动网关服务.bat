@echo off
chcp 65001 >nul
cd /d "%~dp0"
title xinhuo gateway

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] missing .venv
  echo Run setup_env.bat or fix_pip.bat first
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM Incomplete .venv (e.g. copied without packages) -> install requirements
".venv\Scripts\python.exe" -c "import rest_framework" >nul 2>&1
if errorlevel 1 (
  echo [..] .venv missing packages, installing requirements ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --prefer-binary -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --trusted-host files.pythonhosted.org
  if errorlevel 1 (
    echo [..] tsinghua mirror failed, try aliyun ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --prefer-binary -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --trusted-host files.pythonhosted.org
  )
  ".venv\Scripts\python.exe" -c "import rest_framework" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] still missing rest_framework. Run fix_pip.bat then retry.
    pause
    exit /b 1
  )
  echo [OK] requirements installed.
  echo.
)

echo.
echo ============================================================
echo   Slim mode: Django API + WeChat Worker
echo   Web console: http://127.0.0.1:8000/panel/
echo   Keep WeChat logged in. Do NOT close this window.
echo ============================================================
echo.

".venv\Scripts\python.exe" manage.py migrate --noinput
".venv\Scripts\python.exe" manage.py ensure_panel_user

echo [..] start Django on 127.0.0.1:8000 ...
start "xinhuo-Django" /min ".venv\Scripts\python.exe" -m waitress --listen=127.0.0.1:8000 --threads=4 config.wsgi:application

timeout /t 2 /nobreak >nul

echo [..] start Worker ...
start "xinhuo-Worker" /low ".venv\Scripts\python.exe" -m gateway.worker

echo.
echo [OK] Services started.
echo     Open browser: http://127.0.0.1:8000/panel/
echo     Default login (first run): admin / admin123
echo.
echo Tip: open http://127.0.0.1:8000/panel/ in a browser.
echo.
pause
