# One-click deploy on a fresh Windows cloud PC. Idempotent: safe to re-run.
# ASCII-only for Windows PowerShell 5.1. Chinese text is built from code points.
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$EnvFile = Join-Path $Root "config\.env"

function Say([string]$m, [string]$c = "White") { Write-Host $m -ForegroundColor $c }
function Rand([int]$n) {
    $bytes = New-Object byte[] $n
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([BitConverter]::ToString($bytes) -replace "-", "").ToLower()
}

Say "=== [1/6] Check files ===" Cyan
foreach ($f in @("manage.py", "requirements.txt", "setup_env.ps1")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) {
        Say ("  missing: " + $f + "  (unzip the whole package first)") Red
        exit 1
    }
}
Say "  ok" Green

Say "=== [2/6] Python + .venv + packages (skip if already done) ===" Cyan
$needSetup = $true
if (Test-Path -LiteralPath $VenvPy) {
    & $VenvPy -c "import django, rest_framework, waitress, httpx, pywinauto" 2>$null
    if ($LASTEXITCODE -eq 0) { $needSetup = $false; Say "  already installed, skip" Green }
}
if ($needSetup) {
    & powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "setup_env.ps1")
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        Say "  setup failed. See data\remote_setup.log, or run fix_pip.bat" Red
        exit 1
    }
}

Say "=== [3/6] config\.env ===" Cyan
if (Test-Path -LiteralPath $EnvFile) {
    Say "  exists, keep it (not overwritten)" Green
} else {
    New-Item -ItemType Directory -Path (Join-Path $Root "config") -Force | Out-Null
    $tpl = Join-Path $Root ".env.example"
    $lines = @()
    if (Test-Path -LiteralPath $tpl) { $lines = Get-Content -LiteralPath $tpl -Encoding UTF8 }
    $map = @{
        "DJANGO_SECRET_KEY"     = (Rand 32)
        "GATEWAY_HMAC_SECRET"   = (Rand 24)
        "DJANGO_DEBUG"          = "False"
        "WECHAT_POLL_SECONDS"   = "2.5"
        "CHAT_LIST_REFRESH_SECONDS" = "30"
        "REPLY_MIN_INTERVAL_SECONDS" = "0.6"
        "DEDUP_WINDOW_SECONDS"  = "15"
        "SENDER_OCR"            = "0"
    }
    $out = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($ln in $lines) {
        $m = [regex]::Match($ln, '^\s*([A-Z0-9_]+)=')
        if ($m.Success -and $map.ContainsKey($m.Groups[1].Value)) {
            $k = $m.Groups[1].Value
            $out.Add($k + "=" + $map[$k]); $seen[$k] = $true
        } else { $out.Add($ln) }
    }
    foreach ($k in $map.Keys) { if (-not $seen[$k]) { $out.Add($k + "=" + $map[$k]) } }
    [IO.File]::WriteAllLines($EnvFile, $out, (New-Object Text.UTF8Encoding($false)))
    Say "  created with NEW random secrets (DEBUG=False, poll=2.5s)" Green
}

Say "=== [4/6] Database ===" Cyan
New-Item -ItemType Directory -Path (Join-Path $Root "runtime") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Root "data") -Force | Out-Null
& $VenvPy manage.py migrate --noinput 2>&1 | Select-Object -Last 2
& $VenvPy manage.py ensure_panel_user 2>&1 | Select-Object -Last 1

Say "=== [5/6] Self check ===" Cyan
& $VenvPy manage.py check 2>&1 | Select-Object -Last 1
$wx = Get-Process -Name "Weixin", "WeChat" -ErrorAction SilentlyContinue
if ($wx) { Say "  WeChat is running" Green } else { Say "  WeChat NOT running: install + log in before starting the gateway" Yellow }

Say "=== [6/6] Done ===" Cyan
Say "  Next:" White
Say "   1. Log in WeChat, open each group in its own window, pin it (top-left pin)." White
Say "   2. Double-click the start-gateway bat. Open http://127.0.0.1:8000/panel/" White
Say "   3. Login: admin + the password of the packed database. Forgot it? run:  .venv\Scripts\python.exe manage.py changepassword admin" Yellow
Say "   4. Per group: bind members; set 'my group nickname' (see README) so your own messages are recognised." Yellow
