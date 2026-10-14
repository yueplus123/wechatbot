# Remote setup: install Python 3.12 + project .venv (China mirrors)
# ASCII-only for Windows PowerShell 5.1. Place next to manage.py.

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = $ScriptDir
if (-not (Test-Path -LiteralPath (Join-Path $Root "manage.py"))) {
    $parent = Split-Path -Parent $Root
    if (Test-Path -LiteralPath (Join-Path $parent "manage.py")) {
        $Root = $parent
    }
}
Set-Location -LiteralPath $Root

$LogDir = Join-Path $Root "data"
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$Log = Join-Path $LogDir "remote_setup.log"

$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
$PythonVer = "3.12.10"
$PythonUrlCandidates = @(
    "https://mirrors.huaweicloud.com/python/$PythonVer/python-$PythonVer-amd64.exe",
    "https://npmmirror.com/mirrors/python/$PythonVer/python-$PythonVer-amd64.exe",
    "https://www.python.org/ftp/python/$PythonVer/python-$PythonVer-amd64.exe"
)
# Official first: aliyun get-pip is often stale and breaks on Python 3.12 (no distutils)
$GetPipCandidates = @(
    "https://bootstrap.pypa.io/get-pip.py",
    "https://ghproxy.net/https://bootstrap.pypa.io/get-pip.py"
)
$PythonInstallDir = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"

function Write-Log([string]$Msg, [string]$Color = "White") {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Msg"
    Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
    Write-Host $line -ForegroundColor $Color
}

function Invoke-Py {
    param(
        [Parameter(Mandatory = $true)][string]$PyExe,
        [Parameter(Mandatory = $true)][string[]]$ArgList
    )
    $out = & $PyExe @ArgList 2>&1
    foreach ($line in @($out)) {
        $text = "$line"
        if ($text -match '^System\.Management\.Automation\.RemoteException$') { continue }
        Add-Content -LiteralPath $Log -Value $text -Encoding UTF8
        Write-Host $text
    }
    if ($null -eq $LASTEXITCODE) { return 0 }
    return [int]$LASTEXITCODE
}

function Find-PythonExe {
    foreach ($ver in @("3.12", "3.11", "3.10")) {
        try {
            $exe = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $exe = ($exe | Select-Object -Last 1).ToString().Trim()
                if (Test-Path -LiteralPath $exe) { return $exe }
            }
        } catch { }
    }
    foreach ($cand in @(
            (Join-Path $PythonInstallDir "python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
            "C:\Python312\python.exe",
            "C:\Python311\python.exe"
        )) {
        if (Test-Path -LiteralPath $cand) { return $cand }
    }
    try {
        $which = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($which -and (Test-Path -LiteralPath $which.Source)) {
            $v = & $which.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ("$v" -match '^3\.(10|11|12)$') { return $which.Source }
        }
    } catch { }
    return $null
}

function Download-File([string]$Url, [string]$OutFile) {
    Write-Log ("  download: " + $Url) "DarkGray"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 180
        return (Test-Path -LiteralPath $OutFile) -and ((Get-Item -LiteralPath $OutFile).Length -gt 1000)
    } catch {
        Write-Log ("  download fail: " + $_.Exception.Message) "Yellow"
        return $false
    }
}

function Install-Python312 {
    Write-Log "[2/7] Python not found. Installing from China mirrors..." "Cyan"
    $tmp = Join-Path $env:TEMP ("python-" + $PythonVer + "-amd64.exe")
    if (Test-Path -LiteralPath $tmp) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    $ok = $false
    foreach ($url in $PythonUrlCandidates) {
        if (Download-File $url $tmp) {
            if ((Get-Item $tmp).Length -gt 1MB) { $ok = $true; break }
        }
    }
    if (-not $ok) {
        throw "Cannot download Python installer. Install Python 3.12 manually, then re-run."
    }
    $sizeMB = [math]::Round((Get-Item $tmp).Length / 1MB, 1)
    Write-Log ("  installer ready: " + $tmp + " (" + $sizeMB + " MB)") "Green"
    $setupArgs = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_launcher=1",
        "Include_pip=1",
        "Include_test=0",
        "Include_doc=0",
        "SimpleInstall=1",
        ("TargetDir=" + $PythonInstallDir)
    )
    $p = Start-Process -FilePath $tmp -ArgumentList $setupArgs -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
        throw ("Python silent install failed, exit=" + $p.ExitCode)
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $py = Join-Path $PythonInstallDir "python.exe"
    if (-not (Test-Path -LiteralPath $py)) { Start-Sleep -Seconds 2 }
    if (-not (Test-Path -LiteralPath $py)) {
        throw ("python.exe missing after install: " + $py)
    }
    Write-Log ("  Python installed: " + (& $py -c "import sys; print(sys.version)")) "Green"
    return $py
}

function Stop-ProjectPython {
    Write-Log "[3/7] Stop old python..." "Cyan"
    cmd /c "taskkill /F /IM pythonw.exe /T >nul 2>&1"
    cmd /c "taskkill /F /IM python.exe /T >nul 2>&1"
    Start-Sleep -Seconds 1
}

function Remove-Venv {
    Write-Log "[4/7] Remove old .venv if any..." "Cyan"
    if (-not (Test-Path -LiteralPath ".venv")) {
        Write-Log "  no .venv, skip" "DarkGray"
        return
    }
    cmd /c "taskkill /F /IM pythonw.exe /T >nul 2>&1"
    cmd /c "taskkill /F /IM python.exe /T >nul 2>&1"
    Start-Sleep -Seconds 1
    for ($i = 1; $i -le 4; $i++) {
        Write-Log ("  deleting .venv attempt " + $i + "/4 ...") "DarkGray"
        cmd /c "rd /s /q .venv >nul 2>&1"
        if (-not (Test-Path -LiteralPath ".venv")) {
            Write-Log "  .venv removed" "Green"
            return
        }
        Start-Sleep -Seconds 1
    }
    $bak = ".venv_old_to_delete"
    if (Test-Path -LiteralPath $bak) { cmd /c ("rd /s /q " + $bak + " >nul 2>&1") }
    try {
        Rename-Item -LiteralPath ".venv" -NewName $bak -ErrorAction Stop
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c rd /s /q .venv_old_to_delete" -WindowStyle Hidden
        Write-Log "  renamed locked .venv; continue" "Yellow"
    } catch {
        throw "Cannot delete .venv. Run: rd /s /q .venv"
    }
}

function Create-VenvNoPip([string]$BasePy) {
    Write-Log "[5/7] Create .venv..." "Cyan"
    Write-Log ("  base python: " + $BasePy) "DarkGray"
    # Prefer WITH pip (Python 3.12 ensurepip works). Fallback without-pip if hang/fail.
    $p = Start-Process -FilePath $BasePy -ArgumentList @(
        "-m", "venv", ".venv", "--clear"
    ) -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) {
        Write-Log "  venv+pip failed, retry --without-pip ..." "Yellow"
        $p = Start-Process -FilePath $BasePy -ArgumentList @(
            "-m", "venv", ".venv", "--without-pip", "--clear"
        ) -Wait -PassThru -NoNewWindow
        if ($p.ExitCode -ne 0) {
            throw ("venv create failed, exit=" + $p.ExitCode)
        }
    }
    $VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        throw ("missing after venv: " + $VenvPy)
    }
    Write-Log ("  venv python OK: " + (& $VenvPy -c "import sys; print(sys.version)")) "Green"
    return $VenvPy
}

function Bootstrap-Pip([string]$VenvPy) {
    Write-Log "[5b/7] Bootstrap pip into .venv..." "Cyan"
    $rc = Invoke-Py $VenvPy @("-m", "pip", "--version")
    if ($rc -eq 0) {
        Write-Log "  pip already present" "Green"
        return
    }

    Write-Log "  try ensurepip ..." "DarkGray"
    $null = Invoke-Py $VenvPy @("-m", "ensurepip", "--upgrade")
    $rc = Invoke-Py $VenvPy @("-m", "pip", "--version")
    if ($rc -eq 0) {
        Write-Log "  pip via ensurepip OK" "Green"
        return
    }

    Write-Log "  ensurepip missing, download official get-pip.py ..." "Yellow"
    $getPip = Join-Path $env:TEMP "get-pip.py"
    if (Test-Path -LiteralPath $getPip) { Remove-Item -LiteralPath $getPip -Force -ErrorAction SilentlyContinue }
    $ok = $false
    foreach ($url in $GetPipCandidates) {
        if (Download-File $url $getPip) { $ok = $true; break }
    }
    if (-not $ok) {
        throw "Cannot download get-pip.py"
    }
    # Prefer mirror for packages; omit --no-setuptools so wheel/setuptools install
    $rc = Invoke-Py $VenvPy @(
        $getPip,
        "-i", $PipMirror,
        "--trusted-host", "pypi.tuna.tsinghua.edu.cn",
        "--trusted-host", "files.pythonhosted.org",
        "--disable-pip-version-check"
    )
    if ($rc -ne 0) {
        throw "get-pip.py failed"
    }
    $rc = Invoke-Py $VenvPy @("-m", "pip", "--version")
    if ($rc -ne 0) { throw "pip still missing after get-pip" }
    Write-Log "  pip via get-pip OK" "Green"
}

function Install-Requirements([string]$PyExe) {
    Write-Log "[6/7] pip install requirements (China mirrors)..." "Cyan"
    $null = Invoke-Py $PyExe @(
        "-m", "pip", "install", "--upgrade", "pip",
        "--disable-pip-version-check",
        "-i", $PipMirror
    )
    $req = Join-Path $Root "requirements.txt"
    if (-not (Test-Path -LiteralPath $req)) {
        throw "requirements.txt not found."
    }
    $mirrors = @(
        $PipMirror,
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://pypi.douban.com/simple/"
    )
    $ok = $false
    foreach ($m in $mirrors) {
        Write-Log ("  try mirror: " + $m) "DarkGray"
        Write-Log "  this step may take several minutes..." "Yellow"
        $rc = Invoke-Py $PyExe @(
            "-m", "pip", "install", "-r", "requirements.txt",
            "--prefer-binary",
            "--disable-pip-version-check",
            "-i", $m
        )
        if ($rc -eq 0) { $ok = $true; break }
        Write-Log "  mirror failed, try next..." "Yellow"
    }
    if (-not $ok) { throw "pip install requirements.txt failed on all mirrors" }
}

function Write-StartBat {
    $startBat = Join-Path $Root "start_console.bat"
    $lines = @(
        "@echo off",
        "chcp 65001 >nul",
        "cd /d `"%~dp0`"",
        "call `"%~dp0启动网关服务.bat`"",
        "exit /b %ERRORLEVEL%"
    )
    Set-Content -LiteralPath $startBat -Value ($lines -join "`r`n") -Encoding UTF8
}

try {
    "" | Set-Content -LiteralPath $Log -Encoding UTF8
    Write-Log "=== remote setup start ===" "Green"
    Write-Log ("Project: " + $Root)
    Write-Log ("ScriptDir: " + $ScriptDir)

    if (-not (Test-Path -LiteralPath (Join-Path $Root "manage.py"))) {
        throw "manage.py not found. Put this script next to manage.py."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Root "requirements.txt"))) {
        throw "requirements.txt not found."
    }

    Write-Log "[1/7] Detect Python..." "Cyan"
    $BasePy = Find-PythonExe
    if ($BasePy) {
        Write-Log ("  found: " + $BasePy) "Green"
        Write-Log ("  version: " + (& $BasePy -c "import sys; print(sys.version)")) "Green"
    } else {
        $BasePy = Install-Python312
    }

    Stop-ProjectPython
    Remove-Venv

    $VenvPy = Create-VenvNoPip $BasePy
    Bootstrap-Pip $VenvPy
    Install-Requirements $VenvPy

    Write-Log "[7/7] django migrate..." "Cyan"
    $runtimeDir = Join-Path $Root "runtime"
    if (-not (Test-Path -LiteralPath $runtimeDir)) {
        New-Item -ItemType Directory -Path $runtimeDir | Out-Null
    }
    $rc = Invoke-Py $VenvPy @("manage.py", "migrate", "--noinput")
    if ($rc -ne 0) { throw "migrate failed" }

    Write-StartBat

    Write-Log "=== SUCCESS ===" "Green"
    Write-Log "Next: double-click 启动网关服务.bat then 打开网页控制台.bat" "Green"
    Write-Log ("Log: " + $Log) "DarkGray"
    exit 0
} catch {
    Write-Log ("FAILED: " + $_.Exception.Message) "Red"
    Write-Log ("See log: " + $Log) "Yellow"
    exit 1
}