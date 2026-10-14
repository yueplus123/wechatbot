@echo off
chcp 65001 >nul
cd /d "%~dp0"
title fix_pip

echo.
echo === Create .venv and install requirements ===
echo Project: %CD%
echo.

if not exist "manage.py" (
  echo [ERROR] manage.py not found. Open the folder that contains manage.py
  echo Current: %CD%
  pause
  exit /b 1
)

set "PY="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY=%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
  )
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
  )
)

if not defined PY (
  echo [ERROR] Python 3.10+ not found.
  echo Install Python 3.12 from https://www.python.org/downloads/
  echo Check: Add python.exe to PATH
  echo Or run setup_env.bat on a PC that has PowerShell to auto-install.
  pause
  exit /b 1
)

echo [1] base python: %PY%
taskkill /F /IM pythonw.exe /T >nul 2>&1
taskkill /F /IM python.exe /T >nul 2>&1

echo [2] recreate .venv ...
"%PY%" -m venv .venv --clear
if errorlevel 1 (
  echo venv with pip failed, try without-pip ...
  "%PY%" -m venv .venv --without-pip --clear
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv create failed
  pause
  exit /b 1
)

echo [3] ensure pip ...
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
)
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo download official get-pip.py (Python 3.12 needs this, not aliyun stale copy) ...
  del /f /q "%TEMP%\get-pip.py" >nul 2>&1
  "%PY%" -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', r'%TEMP%\get-pip.py')"
  if not exist "%TEMP%\get-pip.py" (
    echo [ERROR] cannot download get-pip.py
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" "%TEMP%\get-pip.py" -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --trusted-host files.pythonhosted.org
)

".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 (
  echo [ERROR] pip still missing
  pause
  exit /b 1
)

echo [4] pip install requirements ...
".venv\Scripts\python.exe" -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --trusted-host files.pythonhosted.org
".venv\Scripts\python.exe" -m pip install -r requirements.txt --prefer-binary -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --trusted-host files.pythonhosted.org --trusted-host mirrors.aliyun.com
if errorlevel 1 (
  echo try aliyun mirror ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --prefer-binary -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --trusted-host files.pythonhosted.org
)
if errorlevel 1 (
  echo [FAIL] pip install requirements failed
  pause
  exit /b 1
)

echo [5] check django ...
".venv\Scripts\python.exe" -c "import django; print('django', django.get_version())"
if errorlevel 1 (
  echo [FAIL] django import failed
  pause
  exit /b 1
)

echo.
echo [OK] Done.
echo Next 1: double-click the start-gateway bat
echo Next 2: open http://127.0.0.1:8000/panel/
echo.
pause
exit /b 0
